from enum import StrEnum


class Role(StrEnum):
    DISCOVERY = "discovery"
    ARCHITECT = "architect"
    REFINER = "refiner"
    TEST_WRITER = "test_writer"
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    QA = "qa"
    MERGER = "merger"
    IMPROVER = "improver"
