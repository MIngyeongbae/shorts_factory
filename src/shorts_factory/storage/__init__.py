"""오브젝트 스토리지 경계 (ADR-0077). 지금은 R2 하나다."""

from .r2 import ObjectStoreError, configured, put_file

__all__ = ["ObjectStoreError", "configured", "put_file"]
