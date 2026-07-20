from datetime import UTC, datetime

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    Column,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Index,
)
from sqlalchemy.types import JSON
from sqlalchemy.orm import relationship

from . import Base


class TestCase(Base):
    __tablename__ = "test_cases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    test_name = Column(String(500), nullable=False)
    test_suite = Column(String(100), nullable=False, index=True)
    module_name = Column(String(100), index=True)
    test_type = Column(String(20), nullable=False, index=True)
    category = Column(String(20), index=True)  # nightly, weekly, e2e-full, other
    hardware = Column(String(20), index=True)
    card_count = Column(Integer)
    file_path = Column(String(500))
    class_name = Column(String(200))
    test_name_hash = Column(String(32), index=True)
    owner = Column(String(100), index=True)
    owner_email = Column(String(100))
    inference_confidence = Column(Float, default=0.0)
    data_granularity = Column(String(20), default="file_level")
    is_flaky = Column(Boolean, default=False, index=True)
    flaky_rate = Column(Float, default=0.0)
    flaky_evidence_count = Column(Integer, default=0)
    flip_count_30d = Column(Integer, default=0)
    pass_rate_7d = Column(Float)
    pass_rate_30d = Column(Float)
    avg_duration_seconds = Column(Float)
    duration_p90_seconds = Column(Float)
    last_pass_duration_seconds = Column(Float)  # 最近一次成功执行的耗时（秒）
    health_score = Column(Float)
    health_level = Column(String(1), index=True)
    first_seen_at = Column(TIMESTAMP)
    last_seen_at = Column(TIMESTAMP)
    last_result = Column(String(20), index=True)
    last_run_at = Column(TIMESTAMP, index=True)
    total_runs = Column(Integer, default=0)
    total_passed = Column(Integer, default=0)
    total_failed = Column(Integer, default=0)
    # 全生命周期累计计数（自用例上线以来），不受 30 天滑动窗口和 90 天数据清理影响
    lifetime_runs = Column(Integer, default=0)
    lifetime_failures = Column(Integer, default=0)
    # 超级管理员可维护字段
    issues_found = Column(Integer, default=0)  # 发现问题数（人工维护值；为 0 时回退到 auto_issues_found）
    suspected_test_issue_count = Column(Integer, default=0)  # 被怀疑为用例自身问题的次数
    is_flaky_manual = Column(Boolean, default=False, index=True)  # 为 True 时 is_flaky 由人工维护，自动检测不再覆盖
    # 自动推导字段（由 IssuesFoundDerivator 定期计算，不可人工编辑）
    auto_issues_found = Column(Integer, default=0)  # 自动推导的发现问题数（CI失败 ↔ BugFix PR 关联）
    auto_suspected_test_issue_count = Column(Integer, default=0)  # 自动推导的疑似用例问题次数
    issues_found_override = Column(Boolean, default=False, index=True)  # 人工是否显式设置过 issues_found
    created_at = Column(TIMESTAMP, default=lambda: datetime.now(UTC))
    updated_at = Column(TIMESTAMP, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))

    runs = relationship("TestRun", back_populates="test_case", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("test_name", "test_suite", "hardware", name="uq_test_case_identity"),
        Index("ix_test_case_module", "module_name", "test_type"),
        Index("ix_test_case_health", "health_level", "is_flaky"),
    )


class TestRun(Base):
    __tablename__ = "test_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    test_case_id = Column(Integer, ForeignKey("test_cases.id"), nullable=False, index=True)
    ci_job_id = Column(BigInteger, index=True)
    ci_run_id = Column(BigInteger, index=True)
    workflow_name = Column(String(100), index=True)
    job_name = Column(String(500))
    result = Column(String(20), nullable=False, index=True)
    duration_seconds = Column(Float)
    model_load_seconds = Column(Float)
    test_exec_seconds = Column(Float)
    failure_category = Column(String(30), index=True)
    failure_message = Column(String(1000))
    flip_detected = Column(Boolean, default=False)
    head_sha = Column(String(40), index=True)
    event = Column(String(50), index=True)
    branch = Column(String(100))
    started_at = Column(TIMESTAMP, index=True)
    completed_at = Column(TIMESTAMP)
    created_at = Column(TIMESTAMP, default=lambda: datetime.now(UTC))

    test_case = relationship("TestCase", back_populates="runs")
    annotations = relationship("FailureAnnotation", back_populates="test_run", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_test_run_case_date", "test_case_id", "started_at"),
        Index("ix_test_run_result", "result", "started_at"),
    )


class TestSuiteSnapshot(Base):
    __tablename__ = "test_suite_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    suite_name = Column(String(100), nullable=False, index=True)
    test_type = Column(String(20), nullable=False, index=True)
    hardware = Column(String(20), index=True)
    card_count = Column(Integer)
    snapshot_date = Column(String(10), nullable=False, index=True)
    total_cases = Column(Integer, default=0)
    passed_cases = Column(Integer, default=0)
    failed_cases = Column(Integer, default=0)
    skipped_cases = Column(Integer, default=0)
    flaky_cases = Column(Integer, default=0)
    pass_rate = Column(Float)
    health_score = Column(Float)
    health_level = Column(String(1))
    avg_duration_seconds = Column(Float)
    duration_p50_seconds = Column(Float)
    duration_p90_seconds = Column(Float)
    total_duration_seconds = Column(Float)
    failure_by_category = Column(JSON)
    created_at = Column(TIMESTAMP, default=lambda: datetime.now(UTC))

    __table_args__ = (
        UniqueConstraint("suite_name", "hardware", "card_count", "snapshot_date", name="uq_suite_snapshot"),
    )


class FailureAnnotation(Base):
    __tablename__ = "failure_annotations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    test_run_id = Column(Integer, ForeignKey("test_runs.id"), nullable=False, index=True)
    annotated_category = Column(String(30), nullable=False, index=True)
    annotated_by = Column(String(100), nullable=False)
    annotation_source = Column(String(20), default="manual")
    created_at = Column(TIMESTAMP, default=lambda: datetime.now(UTC))

    test_run = relationship("TestRun", back_populates="annotations")


__all__ = ["TestCase", "TestRun", "TestSuiteSnapshot", "FailureAnnotation"]
