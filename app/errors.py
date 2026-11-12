from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Ошибка бизнес-логики с кодом из ТЗ и HTTP-статусом."""

    def __init__(
        self,
        code: str,
        status_code: int,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


def validation_error(message: str, **details: Any) -> AppError:
    return AppError("VALIDATION_ERROR", 422, message, details)


def warehouse_archived(name: str) -> AppError:
    return AppError(
        "WAREHOUSE_ARCHIVED",
        409,
        f"ФФ «{name}» в архиве: операция для него недоступна.",
        {"warehouse": name},
    )


def invalid_state(message: str, **details: Any) -> AppError:
    return AppError("INVALID_STATE", 409, message, details)


def name_taken(name: str) -> AppError:
    return AppError(
        "NAME_TAKEN",
        409,
        f"Название «{name}» уже занято активным ФФ.",
        {"name": name},
    )


def total_shortage(message: str, **details: Any) -> AppError:
    return AppError("TOTAL_SHORTAGE", 409, message, details)


def warehouse_shortage(
    warehouse_name: str,
    requested: int,
    available: int,
    total_available: int,
    alternatives: list[dict[str, Any]],
    lines: list[dict[str, Any]] | None = None,
) -> AppError:
    message = (
        f"На ФФ «{warehouse_name}» доступно {available}, запрошено {requested}. "
        f"Суммарно по ФФ доступно {total_available}."
    )
    return AppError(
        "WAREHOUSE_SHORTAGE",
        409,
        message,
        {
            "warehouse": warehouse_name,
            "requested": requested,
            "available": available,
            "total_available": total_available,
            "alternatives": alternatives,
            "lines": lines or [],
            "options": ["choose_other_warehouse", "split", "create_transfer"],
        },
    )
