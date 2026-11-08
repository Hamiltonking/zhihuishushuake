"""DeepSeek 网页搜索客户端

调用 DeepSeek 自研搜索引擎（`POST /v1/tools/search`）为作答补充参考资料。

适用场景：AI 课程的很多知识点只有文本资源、没有 PPT，`_collect_completed_ppts`
拿不到任何参考资料，模型只能凭自己的知识答填空题，容易写成近义概括（如把
「海洋污染」写成「海洋污染治理」）。这里按题干联网搜一次，把摘要当作参考资料
喂给模型，配合 exam_base 的原文锚定使用。

计费：DeepSeek Search 按成功请求计费（约 5 元 / 1000 次），无结果不计费。
文档：https://api-docs.deepseek.com/zh-cn/guides/search
"""

from __future__ import annotations

import re

import httpx
from loguru import logger

from zhs.config import AIConfig

_BLANK_PATTERN = re.compile(r"[_\uff3f\u2014]{2,}|\uff08\s*\uff09|\(\s*\)")


class WebSearchClient:
    """DeepSeek 网页搜索（同步）"""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        count: int = 5,
        min_score: float = 1.5,
        timeout: float = 20.0,
        max_query_len: int = 128,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._count = count
        self._min_score = min_score
        self._timeout = timeout
        self._max_query_len = max_query_len

    @classmethod
    def from_config(cls, ai_config: AIConfig) -> WebSearchClient | None:
        """按 [ai] 配置创建客户端；未开启或没有 api_key 时返回 None"""
        if not ai_config.web_search or not ai_config.api_key:
            return None
        base_url = ai_config.base_url.rstrip("/")
        if base_url.endswith("/v1"):
            base_url = base_url[: -len("/v1")]
        return cls(
            api_key=ai_config.api_key,
            base_url=base_url,
            count=ai_config.web_search_count,
            min_score=ai_config.web_search_min_score,
        )

    @property
    def search_url(self) -> str:
        return f"{self._base_url}/v1/tools/search"

    def search(self, query: str, ori_question: str = "") -> list[dict[str, object]]:
        """搜索网页，返回过滤后的结果列表（失败返回空列表，不抛异常）"""
        payload: dict[str, object] = {"query": query, "count": self._count}
        if ori_question:
            payload["ori_question"] = ori_question

        try:
            response = httpx.post(
                self.search_url,
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=self._timeout,
            )
            response.raise_for_status()
            results = response.json().get("results", [])
        except Exception as e:
            logger.warning(f"联网搜索失败: {e}")
            return []

        if not isinstance(results, list):
            return []
        kept = [r for r in results if isinstance(r, dict) and float(r.get("rerank_score", 0) or 0) >= self._min_score]
        logger.info(f"联网搜索命中 {len(kept)}/{len(results)} 条: {query[:24]}")
        return kept

    def build_materials(self, question: str) -> list[dict[str, str]]:
        """按题干搜索，转成 reference_materials 结构（name/content）"""
        clean = _BLANK_PATTERN.sub(" ", question).strip()
        clean = re.sub(r"\s+", " ", clean)
        if not clean:
            return []
        query = clean[: self._max_query_len]

        materials: list[dict[str, str]] = []
        for item in self.search(query, ori_question=query):
            text = str(item.get("summary") or item.get("snippet") or "").strip()
            if not text:
                continue
            title = str(item.get("title") or "网页").strip()[:40]
            materials.append({"name": f"联网搜索·{title}", "content": text})
        return materials
