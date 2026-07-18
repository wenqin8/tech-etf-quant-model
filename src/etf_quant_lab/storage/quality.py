"""DuckDB persistence for quality reports and their issues."""

from __future__ import annotations

from datetime import date, datetime
from typing import cast

from etf_quant_lab.contracts.enums import QualityGateStatus, QualitySeverity
from etf_quant_lab.contracts.quality import QualityFinding, QualityReport
from etf_quant_lab.ids import IdGenerator
from etf_quant_lab.storage._json import decode_json, encode_json
from etf_quant_lab.storage.duckdb import DuckDBDatabase


class QualityReportRepository:
    """Persist quality reports and issues; one active report per batch."""

    def __init__(self, database: DuckDBDatabase, id_generator: IdGenerator) -> None:
        self._database = database
        self._id_generator = id_generator

    def save(self, report: QualityReport) -> None:
        """Replace any existing report for the batch and its issues atomically."""

        issue_rows = [
            [
                self._id_generator.new(),
                report.report_id,
                report.batch_id,
                finding.rule_code,
                finding.severity.value,
                finding.symbol,
                finding.trade_date,
                encode_json(finding.observed_value),
                encode_json(finding.expected_value),
                finding.message,
                report.generated_at,
            ]
            for finding in report.findings
        ]
        with self._database.transaction() as connection:
            connection.execute(
                """
                DELETE FROM quality_issues
                WHERE batch_id = ?
                """,
                [report.batch_id],
            )
            connection.execute(
                "DELETE FROM quality_reports WHERE batch_id = ?",
                [report.batch_id],
            )
            connection.execute(
                """
                INSERT INTO quality_reports (
                    report_id, batch_id, ruleset_version, gate_status, checked_rows,
                    blocking_count, error_count, warning_count, info_count, generated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    report.report_id,
                    report.batch_id,
                    report.ruleset_version,
                    report.gate_status.value,
                    report.checked_rows,
                    report.count(QualitySeverity.BLOCKING),
                    report.count(QualitySeverity.ERROR),
                    report.count(QualitySeverity.WARNING),
                    report.count(QualitySeverity.INFO),
                    report.generated_at,
                ],
            )
            if issue_rows:
                connection.executemany(
                    """
                    INSERT INTO quality_issues (
                        issue_id, report_id, batch_id, rule_code, severity, symbol,
                        trade_date, observed_value, expected_value, message, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    issue_rows,
                )

    def get_for_batch(self, batch_id: str) -> QualityReport | None:
        """Return the stored report for one batch, or ``None`` when absent."""

        with self._database.read_connection() as connection:
            report_row = connection.execute(
                """
                SELECT
                    report_id, batch_id, ruleset_version, gate_status, checked_rows,
                    generated_at
                FROM quality_reports
                WHERE batch_id = ?
                """,
                [batch_id],
            ).fetchone()
            if report_row is None:
                return None
            issue_rows = connection.execute(
                """
                SELECT
                    rule_code, severity, symbol, trade_date, observed_value,
                    expected_value, message
                FROM quality_issues
                WHERE batch_id = ?
                ORDER BY severity, rule_code, symbol, trade_date
                """,
                [batch_id],
            ).fetchall()
        findings = tuple(_finding_from_row(row) for row in issue_rows)
        return QualityReport(
            report_id=cast(str, report_row[0]),
            batch_id=cast(str, report_row[1]),
            ruleset_version=cast(str, report_row[2]),
            gate_status=QualityGateStatus(cast(str, report_row[3])),
            checked_rows=cast(int, report_row[4]),
            findings=findings,
            generated_at=cast(datetime, report_row[5]),
        )


def _finding_from_row(row: tuple[object, ...]) -> QualityFinding:
    return QualityFinding(
        rule_code=cast(str, row[0]),
        severity=QualitySeverity(cast(str, row[1])),
        message=cast(str, row[6]),
        symbol=cast(str | None, row[2]),
        trade_date=cast(date | None, row[3]),
        observed_value=decode_json(row[4]),
        expected_value=decode_json(row[5]),
    )
