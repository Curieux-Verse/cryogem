"""D-067: the shape of the chain is asserted, not reviewed.

Every invariant here has already been broken once in a way nothing could see
from inside GitHub: a screen built on an hourly collect, a journal that missed a
day, a Pages deploy nobody monitored, a dashboard CI never compiled. The
failures all look like a green run.

The tests read the YAML rather than the comments, so a well-meant edit that
removes the behaviour fails here instead of on the next live run.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from src.db.writes import upsert
from src.report import daily

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"


def load(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def triggers(doc: dict[str, Any]) -> dict[str, Any]:
    # YAML 1.1 reads a bare `on:` key as the boolean True, not the string.
    return doc.get("on", doc.get(True)) or {}


def env_of(step: dict[str, Any]) -> dict[str, Any]:
    return step.get("env") or {}


class TestTriggers:
    def test_nothing_is_scheduled(self):
        """The one non-negotiable: all triggering is external (README, D-031)."""
        for path in sorted(WORKFLOWS.glob("*.yml")):
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            assert "schedule" not in triggers(doc), path.name

    def test_every_job_has_a_timeout(self):
        for path in sorted(WORKFLOWS.glob("*.yml")):
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            for name, job in doc["jobs"].items():
                assert "timeout-minutes" in job, f"{path.name}:{name}"

    def test_the_workflow_run_chain_is_four_workflows_at_most(self):
        """collect-daily -> screen -> publish -> build-site is GitHub's limit of
        three workflow_run levels after a dispatch.

        A fourth link does not error: the last workflow simply never runs, and
        the dashboard sits on stale data looking healthy (D-045).
        """
        parents = {
            path.stem: (
                (triggers(yaml.safe_load(path.read_text(encoding="utf-8"))).get("workflow_run"))
                or {}
            ).get("workflows", [])
            for path in sorted(WORKFLOWS.glob("*.yml"))
        }

        def depth(name: str) -> int:
            return 1 + max((depth(p) for p in parents.get(name, [])), default=0)

        for name in parents:
            assert depth(name) <= 4, f"{name} is {depth(name)} workflows deep"


class TestScreenFollowsADailyCollect:
    def test_collect_daily_reports_its_tier_in_the_run_name(self):
        run_name = load("collect-daily.yml")["run-name"]
        assert "tier=" in run_name and "inputs.tier" in run_name

    def test_screen_requires_tier_daily(self):
        """An hourly collect writes derivatives only. Screening it produced a
        full run stamped today from yesterday's prices."""
        guard = load("screen.yml")["jobs"]["screen"]["if"]
        assert "tier=daily" in guard
        assert "conclusion == 'success'" in guard

    def test_the_tier_marker_cannot_match_an_hourly_run(self):
        """'collect-daily hourly' contains the word 'daily'; 'tier=daily' does
        not appear in it. That is why the marker is spelled with the equals."""
        assert "tier=daily" not in "collect-daily tier=hourly"

    def test_the_guard_is_anchored_at_the_end_of_the_title(self):
        """Unanchored, a crafted tier of 'hourly-but-tier=daily-x' carries the
        marker inside the title and passes a `contains` check (D-068)."""
        guard = load("screen.yml")["jobs"]["screen"]["if"]
        assert "endsWith" in guard and "contains" not in guard

    def test_the_tier_input_is_an_allow_list(self):
        tier = triggers(load("collect-daily.yml"))["workflow_dispatch"]["inputs"]["tier"]
        assert tier["type"] == "choice"
        assert set(tier["options"]) == {"daily", "hourly"}


class TestTheJournalRunsWithTheScreen:
    def test_the_journal_is_a_job_in_screen(self):
        job = load("screen.yml")["jobs"]["journal"]
        assert job["needs"] == "screen"
        # publish triggers on this workflow's conclusion, so a journal gap must
        # not also cost the day's dashboard (D-067).
        assert job["continue-on-error"] is True

    def test_the_journal_job_pings_its_own_check(self):
        steps = load("screen.yml")["jobs"]["journal"]["steps"]
        assert any("HEALTHCHECK_JOURNAL" in str(env_of(s)) for s in steps)

    def test_journal_yml_survives_as_the_manual_path(self):
        assert "workflow_dispatch" in triggers(load("journal.yml"))


class TestMonitoringReachesThePages:
    def test_the_deploy_pings_healthcheck_site(self):
        steps = load("build-site.yml")["jobs"]["deploy"]["steps"]
        assert any("HEALTHCHECK_SITE" in str(env_of(s)) for s in steps)

    def test_build_site_only_deploys_pushes_to_main(self):
        """Pages serves one site: a feature-branch push deployed over the live
        dashboard."""
        assert triggers(load("build-site.yml"))["push"]["branches"] == ["main"]


class TestPublishKeepsTheReport:
    def test_the_report_is_written_and_committed(self):
        job = load("publish.yml")["jobs"]["publish"]
        assert any("src.cli report" in str(s.get("run", "")) for s in job["steps"])
        commit = next(s for s in job["steps"] if "git-auto-commit" in str(s.get("uses", "")))
        pattern = commit["with"]["file_pattern"]
        assert "reports/**" in pattern and "data/public/**" in pattern

    def test_the_date_input_is_passed_as_data_not_interpolated(self):
        """A dispatch input in a run: block is pasted in before the shell parses
        it (D-032)."""
        for step in load("publish.yml")["jobs"]["publish"]["steps"]:
            assert "${{ inputs" not in str(step.get("run", ""))
            if "src.cli publish" in str(step.get("run", "")):
                assert env_of(step)["RUN_DATE"] == "${{ inputs.date }}"


class TestKeysReachTheJobsThatNeedThem:
    def test_the_news_token_reaches_the_hourly_tier(self):
        """`news` is in TIERS['hourly']; without the token news_item stays empty
        and reads as a dead source rather than a missing secret."""
        from src.collectors.registry import TIERS

        assert "news" in TIERS["hourly"]
        steps = load("collect-hourly.yml")["jobs"]["collect"]["steps"]
        assert any("CRYPTOPANIC_AUTH_TOKEN" in env_of(s) for s in steps)

    def test_every_secret_a_workflow_reads_is_documented(self):
        """An undeclared secret is silently empty and its step is skipped
        forever, on a green job (D-065). GITHUB_TOKEN is GitHub's own."""
        documented = set(
            re.findall(r"^([A-Z][A-Z0-9_]+)=", (ROOT / ".env.example").read_text(encoding="utf-8"),
                       re.MULTILINE)
        ) | {"GITHUB_TOKEN"}
        used: set[str] = set()
        for path in sorted(WORKFLOWS.glob("*.yml")):
            used |= set(
                re.findall(r"secrets\.([A-Z][A-Z0-9_]+)", path.read_text(encoding="utf-8"))
            )
        assert used <= documented, f"undocumented: {sorted(used - documented)}"


class TestBackupIsNotPublic:
    def test_no_step_publishes_an_unencrypted_dump(self):
        """An artifact is not a private fallback: on a public repo any signed-in
        account can download a run's artifacts (D-068)."""
        for step in load("backup.yml")["jobs"]["backup"]["steps"]:
            if "upload-artifact" not in str(step.get("uses", "")):
                continue
            assert step["with"]["path"].endswith(".gpg")
            assert step["if"] == "steps.encrypt.outputs.encrypted == 'true'"

    def test_a_missing_passphrase_fails_the_run_and_keeps_nothing(self):
        steps = load("backup.yml")["jobs"]["backup"]["steps"]
        encrypt = next(s for s in steps if s.get("id") == "encrypt")
        assert "::error::" in encrypt["run"]
        assert "exit 1" in encrypt["run"]
        assert "rm -f backup.sql.gz" in encrypt["run"]

    def test_no_release_asset_is_published_unencrypted(self):
        """A release asset on a public repo is public, and the dump is the whole
        database -- journal, holdout audit log and all (D-067)."""
        for step in load("backup.yml")["jobs"]["backup"]["steps"]:
            run = str(step.get("run", ""))
            if "gh release" not in run:
                continue
            assert ".gpg" in run
            assert step["if"] == "steps.encrypt.outputs.encrypted == 'true'"


class TestCIBuildsTheDashboard:
    def test_ci_type_checks_and_builds_web(self):
        steps = load("ci.yml")["jobs"]["web"]["steps"]
        runs = " ".join(str(s.get("run", "")) for s in steps)
        assert "tsc --noEmit" in runs
        assert "npm run build" in runs


class TestOneDateRule:
    """The report, the Telegram summary and the publisher all read the newest
    screen rather than assuming today (D-064, D-067)."""

    def test_no_screen_on_file_is_none_not_today(self, db):
        assert daily.latest_screen_date(db) is None

    def test_the_newest_screen_wins(self, db):
        upsert(
            db,
            "layer1_result",
            [
                {
                    "run_date": date,
                    "base_asset": "BTC",
                    "passed": 1,
                    "failed_checks": "[]",
                    "check_values": "{}",
                    "fetched_at_utc": f"{date}T06:10:00Z",
                }
                for date in ("2026-09-01", "2026-09-03", "2026-09-02")
            ],
        )
        db.commit()
        assert daily.latest_screen_date(db) == "2026-09-03"
