"""PaddleOCR 后端（可选，面向已具备 paddle 环境的团队）。

保留理由：PP-OCRv5 的多语种字典、版面复原（`use_layout=True`）、
表格识别（PP-StructureV3）都只有 paddle 侧有；RapidOCR 的 onnx 版只覆盖 det/cls/rec。

代价（都是实测过的，README 也写了）：
  * 安装 ~700MB；
  * 首次调用联网下载模型（30s+），所以注册表默认**跳过未缓存**的 paddle；
  * 与 PyInstaller 单文件打包很不友好（JIT + 动态库 + 自定义 ops），
    本项目打包脚本默认只带 RapidOCR。

API 兼容性：PaddleOCR 在 2.x/3.x 之间换过两次接口
（`ocr.ocr()` -> `predict()`；返回结构从 `[[box,(text,conf)]]` 变成 dict 列表），
所以这里写了 `_adapt_result` 同时吃三种形态 —— 否则升级 paddle 会静默返回 0 行，
用户只会觉得"怎么识别不出来了"，极难自查。
"""

from __future__ import annotations

import time

from ..logger import get_logger
from ..models import CaptureResult, OcrLine
from . import preprocess
from .base import EngineInfo, OcrEngine, OcrError

log = get_logger("ocr.paddle")


class PaddleOcrEngine(OcrEngine):
    name = "paddle"
    label = "PaddleOCR"

    def __init__(self, cfg):
        super().__init__(cfg)
        self._ocr = None
        self._api = ""        # "predict" | "ocr"，探测到的接口形态

    def warmup(self) -> None:
        if self._ocr is not None:
            return
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise OcrError(f"PaddleOCR 未安装：{exc}", fatal=True,
                           hint="pip install paddleocr paddlepaddle（体积约 700MB）") from exc
        t0 = time.perf_counter()
        kw = dict(use_angle_cls=False, lang=self._lang(), show_log=False,
                  use_gpu=False, det_model_dir=None, rec_model_dir=None)
        try:
            try:
                # 3.x：删掉了 use_angle_cls/show_log，改成新参数名
                self._ocr = PaddleOCR(use_doc_orientation_classify=False,
                                      use_doc_unwarping=False,
                                      use_textline_orientation=False,
                                      lang=self._lang())
                self._api = "predict"
            except TypeError:
                self._ocr = PaddleOCR(**{k: v for k, v in kw.items()
                                         if k not in ("det_model_dir", "rec_model_dir")})
                self._api = "ocr"
        except Exception as exc:
            raise OcrError(f"PaddleOCR 初始化失败：{exc}",
                           hint="首次运行需要联网下载模型；离线环境请预置 ~/.paddleocr") from exc
        log.info("PaddleOCR 加载完成 %.2fs（接口 %s）", time.perf_counter() - t0, self._api)
        self._ready = True

    def _lang(self) -> str:
        """Paddle 的 lang 取值与内部代码不同名，走 languages.ocr_code 映射。"""
        from ..languages import ocr_code

        hint = self.cfg.lang_hint or "ch"
        mapped = ocr_code(hint, "paddle") or hint
        return mapped or "ch"

    def recognize(self, cap: CaptureResult, lang_hint: str = "auto") -> list[OcrLine]:
        self.warmup()
        bgr = self.to_numpy(cap, "bgr")
        prep = preprocess.prepare(bgr, enable=self.cfg.upscale)
        t0 = time.perf_counter()
        try:
            if self._api == "predict":
                raw = list(self._ocr.predict(prep.image))
            else:
                raw = self._ocr.ocr(prep.image, cls=False)
        except Exception as exc:
            raise OcrError(f"PaddleOCR 识别失败：{exc}") from exc
        log.info("PaddleOCR 用时 %.2fs", time.perf_counter() - t0)

        lines: list[OcrLine] = []
        for poly, text, conf in _adapt_result(raw):
            if not text.strip() or conf < self.cfg.min_conf:
                continue
            box = self.poly_to_line(poly)
            inv = 1.0 / prep.scale if prep.scale else 1.0
            phys = tuple(v * inv for v in box)
            lines.append(OcrLine(text=text.strip(), conf=conf,
                                 box=phys, poly=tuple(tuple(p) for p in poly)))
        lines.sort(key=lambda l: (round(l.box[1] / 8), l.box[0]))
        return lines

    @classmethod
    def probe(cls) -> EngineInfo:
        try:
            import paddleocr
            import paddle
            ver = getattr(paddleocr, "__version__", "?")
            cpu = "（CPU 推理）" if not paddle.device.is_compiled_with_cuda() else "（GPU 可用）"
            return EngineInfo(cls.name, cls.label, True, str(ver),
                              f"体积大、首次需下载模型{cpu}")
        except Exception as exc:
            return EngineInfo(cls.name, cls.label, False, "", f"未安装（{exc.__class__.__name__}）")


def _adapt_result(raw):
    """把三种 PaddleOCR 返回形态统一成 (poly, text, conf) 三元组序列。

    1) 2.x  `ocr()`：[[ [poly, (text, conf)], ... ]]  （外层多包一层 list）
    2) 2.7+ `ocr()` 某些版本：[ [poly, [text, conf]], ... ]
    3) 3.x  `predict()`：[{"rec_boxes": [[x0,y0,x1,y1]], "rec_texts": [...],
                                      "rec_scores": [...]}, ...]
    """
    out = []
    if raw is None:
        return out
    for item in raw:
        # 形态 3：dict
        if isinstance(item, dict):
            boxes = item.get("rec_boxes") or item.get("dt_polys") or []
            texts = item.get("rec_texts") or []
            scores = item.get("rec_scores") or []
            for i, t in enumerate(texts):
                if i >= len(boxes):
                    continue
                b = boxes[i]
                poly = ([[b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]]]
                        if len(b) == 4 and not isinstance(b[0], (list, tuple)) else b)
                out.append((poly, t, float(scores[i]) if i < len(scores) else 1.0))
            continue
        # 形态 1/2：list，可能再嵌套一层，统一拍平成 row 列表
        rows = _flatten(item)
        for entry in rows:
            try:
                poly, pair = entry[0], entry[1]
                if isinstance(pair, (list, tuple)) and len(pair) == 2:
                    text, conf = pair[0], float(pair[1])
                else:
                    text, conf = str(pair), 1.0
            except (IndexError, TypeError, ValueError):
                continue
            if not isinstance(poly, (list, tuple)) or len(poly) < 4:
                continue
            out.append((poly, text, conf))
    return out


def _flatten(item) -> list:
    """把 [[row, row, ...]] 与 [row, row, ...] 都拍平成 row 列表。"""
    if not isinstance(item, (list, tuple)):
        return []
    if len(item) == 2 and isinstance(item[0], (list, tuple)) and _is_poly(item[0]):
        return [item]
    out = []
    for sub in item:
        out.extend(_flatten(sub) if isinstance(sub, (list, tuple)) and not _is_poly(sub)
                   else ([sub] if isinstance(sub, (list, tuple)) else []))
    return out


def _is_poly(x) -> bool:
    """poly 是 4 个 [num, num] 点。"""
    if not isinstance(x, (list, tuple)) or len(x) < 4:
        return False
    first = x[0]
    return (isinstance(first, (list, tuple)) and len(first) == 2
            and isinstance(first[0], (int, float)))
