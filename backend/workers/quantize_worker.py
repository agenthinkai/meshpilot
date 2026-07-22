"""Celery worker — model quantization task."""

from __future__ import annotations

import asyncio
from core.celery_app import celery_app


@celery_app.task(name="workers.quantize_worker.quantize_model_task", bind=True, max_retries=1)
def quantize_model_task(self, model_id: str, file_path: str):
    """Run quantization for an uploaded model."""
    asyncio.run(_async_quantize(model_id, file_path))


async def _async_quantize(model_id: str, file_path: str):
    from core.quantizer import AutoQuantizer
    from core.database import AsyncSessionLocal, ModelRecord, ModelStatus, QuantBits
    from core.cpu_detect import get_cpu_profile
    from sqlalchemy import select

    quantizer = AutoQuantizer(model_id, file_path)
    result = quantizer.run()

    async with AsyncSessionLocal() as db:
        res = await db.execute(select(ModelRecord).where(ModelRecord.id == model_id))
        record = res.scalar_one_or_none()
        if not record:
            return

        if result.success:
            record.status    = ModelStatus.READY
            record.file_path = result.output_path
            if result.quant_bits:
                try:
                    record.quant_bits = QuantBits(result.quant_bits)
                except ValueError:
                    pass
            cpu = get_cpu_profile()
            record.backend = cpu.recommended_backend
        else:
            record.status    = ModelStatus.ERROR
            record.metadata_ = {"quantization_error": result.error}

        await db.commit()
