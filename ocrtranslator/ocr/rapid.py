"""RapidOCR（PP-OCR + ONNXRuntime）后端 —— 默认推荐引擎。

为什么选它而不是 PaddleOCR / Tesseract（对比数据见 README）：

    维度           RapidOCR(onnxruntime)      PaddleOCR(gpu=False)     Tesseract
    ──────────────────────────────────────────────────────────────────────────────
    安装体积         ~17MB wheel + 3 个 onnx      ~700MB（paddle 框架）    ~40MB + 语言包
    模型来源         内置在 wheel 里，装完即离线    首次运行联网下载         需装 tesseractsetup.exe
    加载耗时（实测）   1.2s                        8~25s                  ~0.1s
    单次识别（实测）   0.48s（限线程后）             2~6s                  0.15~0.6s
    中文精度          PP-OCRv4，强                 同左（同一模型）         明显弱
    中英混排          一行内混排 OK                 一行内混排 OK           需切 lang，且丢空格
    单文件打包         可行（纯 python + onnx）      极难（JIT + 动态库）     可行但要外带 exe
    许可证           Apache-2.0                  Apache-2.0              CPML
    语种数            中/英/日/韩等（模型字典决定）    80+                   400+

    结论：一个"双击就能用、要出单 exe"的桌面工具，RapidOCR 是唯一同时满足
    中文精度、离线、可打包三项的选择。PaddleOCR 保留为可插拔后端
    （团队已有 paddle 环境、或需要 PP-OCRv5 更多语种/版面复原时切过去）；
    Tesseract 保留给 CPU 很弱的老机器与冷门语种（400+）。

    性能的两个关键开关（都是实测得来，见 ocr/preprocess.py 模块 docstring）：
      * `intra_op_num_threads`：默认 -1（全核）实测 1661ms，限到 4 反而 484ms；
      * `det_limit_type="max"`：默认 "min" 会把小图内部放大到短边 736，
        与我们自己的 2x 放大叠成二次重采样，正是细笔画糊掉的来源。
"""

from __future__ import annotations

import os
import threading
import time

from ..logger import get_logger
from ..models import CaptureResult, OcrLine
from . import preprocess
from .base import EngineInfo, OcrEngine, OcrError

log = get_logger("ocr.rapid")

_DEFAULT_THREADS = 4


class RapidOcrEngine(OcrEngine):
    name = "rapid"
    label = "RapidOCR (PP-OCR + ONNX)"

    def __init__(self, cfg):
        super().__init__(cfg)
        self._eng = None
        self._lock = threading.Lock()
        self.last_elapsed: tuple[float, ...] = ()

    # -------------------------------------------------- 生命周期
    def warmup(self) -> None:
        if self._eng is not None:
            return
        with self._lock:
            if self._eng is not None:
                return
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError as exc:
                raise OcrError(
                    f"RapidOCR 未安装：{exc}",
                    fatal=True,
                    hint="pip install rapidocr-onnxruntime  或  python tools/fetch_wheels.py",
                ) from exc
            t0 = time.perf_counter()
            nthreads = self.cfg.threads or _thread_default()
            try:
                # kwargs 命名规则（见 rapidocr_onnxruntime/utils/parse_parameters.py）：
                # det_/cls_/rec_ 前缀写入对应子配置，无前缀写入 Global。
                self._eng = RapidOCR(
                    det_limit_type="max",       # 不做内部放大，尺寸交给 preprocess
                    det_limit_side_len=1600,
                    det_box_thresh=0.45,
                    det_unclip_ratio=1.7,
                    text_score=0.30,            # 宁多勿少，噪声行由 min_conf/_is_noise 兜
                    intra_op_num_threads=nthreads,
                    inter_op_num_threads=1,
                    print_verbose=False,
                )
            except Exception as exc:
                raise OcrError(f"RapidOCR 初始化失败：{exc}",
                               hint="检查 onnxruntime 是否与当前 Python 版本匹配") from exc
            self.last_threads = nthreads
            log.info("RapidOCR 加载完成 %.2fs（推理线程 %d）", time.perf_counter() - t0, nthreads)
            self._ready = True

    def close(self) -> None:
        self._eng = None
        self._ready = False

    # -------------------------------------------------- 识别
    def recognize(self, cap: CaptureResult, lang_hint: str = "auto") -> list[OcrLine]:
        """识别一张裁剪图。坐标返回**物理像素**，由调用方除以 DPR 得逻辑坐标。

        `lang_hint` 对 RapidOCR 无实际作用：onnx 版用的是 PP-OCR 的多语种字典
        （中/英/日/韩/实在同一模型内），不像 Tesseract 需要选语言包。
        保留参数只为与 OcrEngine 接口一致，以及未来接多模型字典时可用。
        """
        self.warmup()
        assert self._eng is not None, "warmup 后仍为 None"
        bgr = self.to_numpy(cap, "bgr")
        # cfg.upscale 语义 = "启用预处理策略"。策略本身一律放大 2.0x，
        # 只按估计行高切换插值方式（实测放大是净收益，见 preprocess.py 结论 1）。
        prep = preprocess.prepare(bgr, enable=self.cfg.upscale)
        log.debug("预处理：%s (scale=%.2f -> %dx%d)", prep.reason, prep.scale,
                  prep.image.shape[1], prep.image.shape[0])

        # 只有真要用空格修复时才取 word box：cal_rec_boxes 是额外一趟逐字符重投影，
        # round2 实测该修复净收益 +0（字符框是等距铺开的，间隙不含空白信息），
        # 所以默认不付这个代价。
        want_words = self.cfg.restore_spaces
        t0 = time.perf_counter()
        try:
            res, elapsed = self._eng(prep.image,
                                     return_word_box=want_words,
                                     use_det=self.cfg.use_det,
                                     use_cls=False,     # 截图不存在整屏 180° 旋转，省一趟分类
                                     use_rec=self.cfg.use_rec)
        except Exception as exc:
            raise OcrError(f"RapidOCR 推理失败：{exc}") from exc
        self.last_elapsed = tuple(elapsed or ())
        log.info("RapidOCR 识别 %dx%d 用时 %.2fs，%d 行",
                 prep.image.shape[1], prep.image.shape[0],
                 time.perf_counter() - t0, len(res or []))

        lines: list[OcrLine] = []
        for row in res or []:
            # return_word_box=True 时 row = [det_box, text, conf, char_boxes, chars, char_confs]
            # （见 rapidocr_onnxruntime/main.py: ocr_res = [[box.tolist(), *res] ...]）
            # 关闭时只有前 3 项，因此必须按长度取值，不能写死下标。
            try:
                poly, text, conf = row[0], row[1], float(row[2])
            except (IndexError, TypeError, ValueError):
                log.warning("异常识别结果结构，跳过：%r", str(row)[:120])
                continue
            if not text or not text.strip():
                continue
            if conf < self.cfg.min_conf:
                continue
            if self.cfg.drop_punctuation_only and _is_noise(text):
                continue
            if want_words and len(row) >= 6 and isinstance(row[3], list) and row[3]:
                text = preprocess.repair_spaces(text, row[3])
            box = self.poly_to_line(poly)
            inv = 1.0 / prep.scale if prep.scale else 1.0
            phys = tuple(v * inv for v in box)
            poly_s = tuple((float(p[0]) * inv, float(p[1]) * inv) for p in poly)
            lines.append(OcrLine(text=text.strip(), conf=conf,
                                 box=(phys[0], phys[1], phys[2], phys[3]), poly=poly_s))
        lines.sort(key=lambda l: (round(l.box[1] / 8), l.box[0]))
        return lines

    # -------------------------------------------------- 探测
    @classmethod
    def probe(cls) -> EngineInfo:
        try:
            import rapidocr_onnxruntime as m
            ver = getattr(m, "__version__", "") or ""
            if not ver:
                try:
                    from importlib.metadata import version
                    ver = version("rapidocr-onnxruntime")
                except Exception:
                    ver = "?"
            note = "完全离线，模型内置于 wheel"
            try:
                import onnxruntime as ort
                extra = [p for p in ort.get_available_providers() if p != "CPUExecutionProvider"]
                if extra:
                    note += f"；可用加速后端 {', '.join(extra)}"
            except Exception:
                pass
            return EngineInfo(cls.name, cls.label, True, str(ver), note)
        except Exception as exc:
            return EngineInfo(cls.name, cls.label, False, "",
                              f"未安装（{exc.__class__.__name__}）")


def _thread_default() -> int:
    """实测最优：4 线程优于全核（1661ms -> 484ms），也避免吃满 CPU 卡住 GUI。

    小核机器（2 核）就取 2；不再往上加，因为 OpenMP 在核间的同步开销
    大于 PP-OCR 这种小 batch 推理的并行收益。
    """
    return max(2, min(_DEFAULT_THREADS, (os.cpu_count() or 4) // 2))


_NOISE_CHARS = set("·。，、；：？！“”‘’（）《》〈〉【】〔〕—…_,.;:!?\"'`()[]{}<>-~`|/\\@#*+^%$&")


def _is_noise(text: str) -> bool:
    """只含标点/符号或单字符的行直接丢弃。

    截图里图标常被识别成一两个符号，占位但无意义。送进翻译器会产出
    "。" -> "." 这种垃圾配对，还会在双语对照列表里挤掉真正的内容，
    所以在 OCR 出口就剔除，不留给 UI 层判断。
    """
    s = text.strip()
    if len(s) <= 1:
        return True
    return all(c in _NOISE_CHARS or c.isspace() for c in s)
