import asyncio
import logging

from app.config import Settings
from app.rag import HybridRetriever

logger = logging.getLogger(__name__)


async def main() -> None:
    retriever = await HybridRetriever.connect(Settings())
    try:
        summary = await retriever.ingest()
        logger.info(
            "Knowledge ingestion: inserted=%d skipped=%d deleted=%d",
            summary["inserted"],
            summary["skipped"],
            summary["deleted"],
        )
    finally:
        await retriever.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
