from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from core.verify.models import (
    CheckResult,
    EvidenceBundle,
    FieldComparison,
    VerificationOutcome,
)

logger = logging.getLogger(__name__)


class EvidenceCollector:
    """
    Assembles, formats, and persists evidence bundles for independent verification.
    """

    def __init__(self, artifacts_dir: Path | str = "artifacts") -> None:
        self.artifacts_dir = Path(artifacts_dir)

    def assemble_bundle(
        self,
        run_id: str,
        task: str,
        outcome: VerificationOutcome,
        checks: list[CheckResult],
        discrepancies: list[str],
        extracted_fields_table: list[FieldComparison],
        system_state_before: dict[str, Any] | None = None,
        system_state_after: dict[str, Any] | None = None,
        screenshot_paths: list[str] | None = None,
        artifact_refs: list[str] | None = None,
    ) -> EvidenceBundle:
        screenshots = list(screenshot_paths or [])

        # Auto-discover any screenshots in artifacts directory for this run
        if not screenshots and self.artifacts_dir.exists():
            for p in self.artifacts_dir.glob(f"**/*{run_id}*"):
                if p.suffix.lower() in {".png", ".jpg", ".jpeg"}:
                    screenshots.append(str(p))

        bundle = EvidenceBundle(
            run_id=run_id,
            task=task,
            outcome=outcome,
            discrepancies=discrepancies,
            checks=checks,
            extracted_fields_table=extracted_fields_table,
            screenshots=screenshots,
            artifacts=list(artifact_refs or []),
            system_state_before=system_state_before or {},
            system_state_after=system_state_after or {},
        )

        # Persist bundle to artifacts directory
        try:
            run_bundle_dir = self.artifacts_dir / run_id
            run_bundle_dir.mkdir(parents=True, exist_ok=True)

            json_path = run_bundle_dir / "evidence_bundle.json"
            json_path.write_text(json.dumps(bundle.model_dump(), indent=2), encoding="utf-8")

            md_path = run_bundle_dir / "evidence_bundle.md"
            md_path.write_text(bundle.to_markdown(), encoding="utf-8")
            logger.info(f"Persisted evidence bundle for run {run_id} to {json_path}")
        except Exception as e:
            logger.warning(f"Failed to persist evidence bundle to disk: {e}")

        return bundle
