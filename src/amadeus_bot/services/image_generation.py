from __future__ import annotations

import asyncio
import hashlib
import io
from pathlib import Path

from PIL import Image

from amadeus_bot.adapters.ai_provider import AIProviderRateLimited
from amadeus_bot.domain.ai import AITask
from amadeus_bot.services.ai_router import AIQuotaExceeded, AIService


class ImageGenerationService:
    def __init__(self, ai: AIService, directory: Path) -> None:
        self.ai = ai
        self.directory = directory
        self._lock = asyncio.Lock()

    async def generate(self, prompt: str, user_id: str, group_id: str | None = None) -> Path:
        prompt = prompt.strip()
        if not prompt or len(prompt) > 2000:
            raise ValueError("画面描述必须为 1～2000 字符")
        if self._lock.locked():
            raise ValueError("正在生成其他图片，请稍后再试")
        async with self._lock:
            try:
                result = await self.ai.complete(
                    AITask.IMAGE_GENERATION,
                    [{"role": "user", "content": prompt}],
                    user_id=user_id,
                    group_id=group_id,
                )
            except AIProviderRateLimited:
                raise ValueError("图像供应商当前配额已耗尽或限流，请稍后重试") from None
            except AIQuotaExceeded:
                raise ValueError("图像生成已达到今日调用上限") from None
            if not result.images:
                raise ValueError("图像服务未返回图片，可能拒绝了该请求")
            return await asyncio.to_thread(self._save, result.images[0])

    def _save(self, data: bytes) -> Path:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"} or image.width * image.height > 25_000_000:
                raise ValueError("图像服务返回了不支持的图片")
            suffix = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}[image.format]
            image.verify()
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / (hashlib.sha256(data).hexdigest() + suffix)
        if not path.exists():
            path.write_bytes(data)
        return path
