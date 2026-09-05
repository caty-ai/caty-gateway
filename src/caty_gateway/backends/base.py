from abc import ABC, abstractmethod
from typing import Iterator, Optional


class Backend(ABC):
    @abstractmethod
    def generate(self, text: str, session_id: Optional[str], timeout: int, route: Optional[str] = None, attachments=None) -> str:
        raise NotImplementedError

    @abstractmethod
    def stream(self, text: str, session_id: Optional[str], timeout: int, route: Optional[str] = None, attachments=None) -> Iterator[str]:
        raise NotImplementedError

    def supports_stream(self) -> bool:
        return False

    def attachment_transports(self) -> frozenset[str]:
        return frozenset()

    def supported_attachment_mimes(self) -> frozenset[str]:
        return frozenset()

    def attachment_max_bytes(self) -> Optional[int]:
        return None

    def attachment_staging_dir(self) -> Optional[str]:
        return None

    def list_external(self, limit: int = 30) -> list:
        return []

    def read_external(self, native_id: str, limit: int = 50) -> list:
        return []
