"""Pure routing policy and the local Open Jev scoring adapter."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import math
from pathlib import Path
from pathlib import PurePosixPath
import re
import subprocess
from typing import Any

import httpx


MODELS = (
    "gpt-6-luna",
    "gpt-5.6-terra",
    "gpt-6-sol",
    "gpt-6-astra",
)
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_MODEL = "gpt-6-sol"
DEFAULT_EFFORT = "medium"


@dataclass(frozen=True)
class Candidate:
    model: str
    effort: str

    @property
    def key(self) -> str:
        return f"{self.model}/{self.effort}"

    @property
    def requires_confirmation(self) -> bool:
        return self.model == "gpt-6-astra"


@dataclass(frozen=True)
class Profile:
    kind: str
    scope: str
    file_count: int
    languages: tuple[str, ...]
    existing_changes: bool


@dataclass(frozen=True)
class ImplementationContract:
    """A reviewed, bounded implementation brief supplied by the coordinator."""

    requirement: str
    allowed_paths: tuple[str, ...]
    acceptance_checks: tuple[str, ...]
    existing_patterns: tuple[str, ...]
    decisions_resolved: bool
    risk: str
    ui_baseline: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ImplementationContract:
        if not isinstance(payload, dict):
            raise ValueError("Implementation contract must be a JSON object")
        required = {
            "requirement", "allowed_paths", "acceptance_checks", "existing_patterns",
            "decisions_resolved", "risk",
        }
        if missing := required - payload.keys():
            raise ValueError(f"Implementation contract is missing: {', '.join(sorted(missing))}")
        if unknown := payload.keys() - required - {"ui_baseline"}:
            raise ValueError(f"Unknown implementation contract fields: {', '.join(sorted(unknown))}")
        if not isinstance(payload["requirement"], str) or not payload["requirement"].strip():
            raise ValueError("Implementation requirement must be nonempty text")
        if not isinstance(payload["decisions_resolved"], bool):
            raise ValueError("decisions_resolved must be a boolean")
        if payload["risk"] not in ("low", "medium", "high"):
            raise ValueError("risk must be low, medium, or high")

        def strings(name: str) -> tuple[str, ...]:
            value = payload.get(name, [])
            if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
                raise ValueError(f"{name} must be a list of nonempty strings")
            return tuple(item.strip() for item in value)

        return cls(
            payload["requirement"].strip(), strings("allowed_paths"),
            strings("acceptance_checks"), strings("existing_patterns"),
            payload["decisions_resolved"], payload["risk"], strings("ui_baseline"),
        )


@dataclass(frozen=True)
class Decision:
    candidate: Candidate
    reason: str
    score: float | None
    compared: int
    profile: Profile
    policy_band: str = "unrestricted"

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["candidate"]["key"] = self.candidate.key
        result["candidate"]["requires_confirmation"] = self.candidate.requires_confirmation
        return result


def _value(item: Any) -> str:
    return str(getattr(item, "value", item))


def available_candidates(model_entries: list[Any]) -> list[Candidate]:
    """Use the live Codex catalog as the authority for supported pairs."""
    supported: dict[str, set[str]] = {}
    for entry in model_entries:
        if bool(getattr(entry, "hidden", False)):
            continue
        model = _value(getattr(entry, "model", getattr(entry, "id", "")))
        if model not in MODELS:
            continue
        options = getattr(entry, "supported_reasoning_efforts", None) or []
        efforts = {
            _value(getattr(option, "reasoning_effort", option)) for option in options
        }
        supported[model] = efforts
    return [
        Candidate(model, effort)
        for model in MODELS
        for effort in EFFORTS
        if effort in supported.get(model, set())
    ]


def fallback(candidates: list[Candidate]) -> Candidate:
    """A safe deterministic default; never selects Astra on scoring failure."""
    non_astra = [c for c in candidates if not c.requires_confirmation]
    if not non_astra:
        raise RuntimeError("No non-Astra model and reasoning pair is available")
    priority = {"gpt-6-sol": 0, "gpt-5.6-terra": 1, "gpt-6-luna": 2}
    effort_priority = {"medium": 0, "low": 1, "high": 2, "xhigh": 3, "max": 4}
    return min(non_astra, key=lambda c: (priority[c.model], effort_priority[c.effort]))


def _policy_fallback(candidates: list[Candidate]) -> Candidate:
    if candidates and all(candidate.requires_confirmation for candidate in candidates):
        return next((candidate for candidate in candidates if candidate.effort == "medium"), candidates[0])
    return fallback(candidates)


def _policy_band(request: str, profile: Profile) -> str:
    """Conservative first-pass bounds; task results must calibrate these later."""
    lower = request.lower()
    if re.search(r"^\s*(?:please\s+)?(?:use|run|choose)\s+(?:gpt[- ]?6[- ]?)?astra\b", lower) or re.search(r"^\s*(?:gpt[- ]?6[- ]?)?astra로\s*(?:분석|작업|진행|해|봐)", lower):
        return "explicit_astra"
    critical = (
        "race", "concurren", "duplicate payout", "rollback", "migration",
        "authorization", "privilege escalation", "security", "settlement",
        "경쟁 조건", "동시성", "정산", "권한", "마이그레이션", "롤백",
    )
    critical_hits = sum(term in lower for term in critical)
    systemic = bool(re.search(r"across\b.*\bservices\b", lower)) or any(
        term in lower for term in ("multi-module", "old and new api", "multiple subsystems", "여러 서비스", "전 시스템")
    )
    if profile.scope == "broad" and ("very difficult" in lower or "매우 어려운" in lower or (critical_hits >= 3 and systemic)):
        return "frontier_plan"
    if profile.scope == "broad" and critical_hits >= 2 and systemic:
        return "frontier"
    if critical_hits:
        return "critical"
    small = (
        "typo", "spelling", "button label", "rename one", "optional field",
        "오탈자", "문구 수정", "이름 변경", "단순 수정",
    )
    localized_setting = bool(
        re.search(r"\b(?:one|single)\b.*\b(?:configuration|config|setting)\b.*\bfile\b", lower)
        or re.search(r"\b(?:one|single)\b.*\b(?:config|setting|constant|unit test assertion)\b", lower)
        or re.search(r"\b(?:config|setting|constant)\b.*\b(?:one|single)\b.*\bfile\b", lower)
        or re.search(r"(?:설정|상수).*(?:하나|한 개|한 파일)", lower)
    )
    spread = any(term in lower for term in (
        "across", "multiple", "multi-module", "several", "entire", "migration",
        "refactor", "architecture", "여러", "전체", "전면", "마이그레이션", "리팩터", "아키텍처",
    ))
    if profile.scope == "focused" and not spread and (localized_setting or any(term in lower for term in small)):
        return "small"
    if profile.scope == "broad" or any(term in lower for term in ("architecture", "redesign the entire", "아키텍처", "전면 개편")):
        return "complex"
    multi_part = (
        "across", "pagination", "modal", "loading", "validation",
        "responsive", "accessible", "n+1", "service and repository",
        "여러 모듈", "여러 화면", "접근성", "반응형",
    )
    if any(term in lower for term in multi_part):
        return "standard"
    return "unclassified"


def _bounded_implementation(contract: ImplementationContract, request: str, profile: Profile) -> bool:
    """A contract can lower implementation effort only after deterministic safety checks."""
    if not contract.decisions_resolved or contract.risk != "low":
        return False
    if not contract.requirement.strip() or not contract.existing_patterns or not contract.acceptance_checks:
        return False
    if not 1 <= len(contract.allowed_paths) <= 4 or len(set(contract.allowed_paths)) != len(contract.allowed_paths):
        return False
    for path in contract.allowed_paths:
        parsed = PurePosixPath(path)
        if (not path or path.startswith(("/", "~")) or "\\" in path
                or any(part in ("", ".", "..") for part in path.split("/"))
                or any(char in path for char in "*?[]") or parsed.is_absolute()):
            return False
    if profile.kind in ("frontend", "mixed") and not contract.ui_baseline:
        return False
    critical = (
        "security", "authorization", "authentication", "permission", "privilege",
        "concurren", "race condition", "migration", "schema change", "payment",
        "settlement", "payout", "rollback", "transaction boundary", "encryption",
        "보안", "인증", "인가", "권한", "동시성", "경쟁 조건", "마이그레이션",
        "스키마 변경", "결제", "정산", "송금", "롤백", "트랜잭션 경계", "암호화",
    )
    content = " ".join((
        request, contract.requirement, *contract.existing_patterns,
        *contract.acceptance_checks, *contract.ui_baseline,
    )).lower()
    if any(term in content for term in critical):
        return False
    return True


def _eligible(candidate: Candidate, band: str) -> bool:
    model, effort = candidate.model, candidate.effort
    if band == "explicit_astra":
        return model == "gpt-6-astra"
    if band == "small":
        return model in ("gpt-6-luna", "gpt-5.6-terra") and effort in ("low", "medium")
    if band == "standard":
        return (model == "gpt-5.6-terra" and effort in ("medium", "high")) or (model == "gpt-6-sol" and effort == "medium")
    if band == "bounded_implementation":
        return model in ("gpt-6-luna", "gpt-6-sol") and effort == "medium"
    if band == "complex":
        return model == "gpt-6-sol" and effort in ("medium", "high", "xhigh", "max")
    if band == "replan_review":
        return model == "gpt-6-sol" and effort in ("high", "xhigh")
    if band == "frontier":
        return (model == "gpt-6-sol" and effort in ("high", "xhigh", "max")) or (model == "gpt-6-astra" and effort in ("low", "medium", "high"))
    if band == "frontier_plan":
        return model == "gpt-6-astra" and effort in ("low", "medium", "high")
    return model == "gpt-6-sol" and effort in ("high", "xhigh", "max")


def inspect_repo(path: Path, request: str) -> Profile:
    files = subprocess.run(
        ["git", "-C", str(path), "ls-files", "--cached", "--others", "--exclude-standard"],
        capture_output=True, text=True, check=True, timeout=10,
    ).stdout.splitlines()
    statuses = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain"],
        capture_output=True, text=True, check=True, timeout=10,
    ).stdout
    counts: dict[str, int] = {}
    language_extensions = {
        ".py": "Python", ".rs": "Rust", ".kt": "Kotlin", ".java": "Java",
        ".ts": "TypeScript", ".tsx": "React", ".js": "JavaScript",
        ".jsx": "React", ".vue": "Vue", ".swift": "Swift",
    }
    for file in files[:5000]:
        language = language_extensions.get(Path(file).suffix.lower())
        if language:
            counts[language] = counts.get(language, 0) + 1
    terms = request.lower()
    front = any(x in terms for x in ("ui", "ux", "화면", "디자인", "frontend", "프론트", "react", "css"))
    back = any(x in terms for x in ("api", "db", "서버", "백엔드", "backend", "database", "service"))
    kind = "mixed" if front and back else "frontend" if front else "backend" if back else "general"
    broad = any(x in terms for x in ("리팩터", "전면", "아키텍처", "migration", "마이그레이션", "전체", "설계"))
    scope = "broad" if broad else "focused"
    return Profile(kind, scope, len(files), tuple(sorted(counts, key=counts.get, reverse=True)[:4]), bool(statuses))


def _option(candidate: Candidate) -> str:
    tier = {
        "gpt-6-luna": "fast model for straightforward work",
        "gpt-5.6-terra": "balanced older model for routine work",
        "gpt-6-sol": "coding workhorse for implementation and debugging",
        "gpt-6-astra": "frontier model for demanding work; explicit user confirmation required",
    }[candidate.model]
    depth = {
        "low": "light reasoning",
        "medium": "normal reasoning",
        "high": "deeper reasoning",
        "xhigh": "very deep reasoning",
        "max": "maximum reasoning",
    }[candidate.effort]
    return f"{candidate.key}: {tier}; {depth}"


def _context(request: str, profile: Profile) -> str:
    return (
        "Choose one Codex configuration for the user's coding task. "
        "Prefer enough capability and reasoning to complete it, while avoiding unnecessary cost. "
        "All choices are available in this account. Score relative suitability, not success probability.\n"
        f"Request: {request[:4000]}\n"
        f"Task type: {profile.kind}; scope: {profile.scope}; "
        f"repository files: {profile.file_count}; languages: {', '.join(profile.languages) or 'unknown'}; "
        f"existing changes: {profile.existing_changes}.\n"
        "Selected configuration:"
    )


async def _score(client: httpx.AsyncClient, url: str, context: str, options: list[str], norm: str = "pmi") -> list[tuple[float, float]]:
    response = await client.post(
        f"{url.rstrip('/')}/score",
        json={"context": context, "options": options, "norm": norm, "chat": False, "sep": ""},
    )
    response.raise_for_status()
    data = response.json()
    index = data.get("best_index")
    returned = data.get("options")
    if not isinstance(index, int) or not 0 <= index < len(options) or not isinstance(returned, list) or len(returned) != len(options):
        raise ValueError("Open Jev returned an invalid option ranking")
    if any(not isinstance(item, dict) or item.get("option") != option for item, option in zip(returned, options)):
        raise ValueError("Open Jev returned options in an unexpected order")
    scores = [(float(item["score"]), float(item["probability"])) for item in returned]
    if any(not math.isfinite(score) or not math.isfinite(probability) for score, probability in scores):
        raise ValueError("Open Jev returned non-finite scores")
    return scores


async def _classify_unfamiliar_task(client: httpx.AsyncClient, url: str, request: str) -> str:
    """Use an option menu to recognize small edits beyond fixed keywords."""
    options = [
        " a localized edit with no design decisions",
        " a standard feature involving several components",
        " a complex change across system boundaries",
        " an exceptional problem requiring strategic planning",
    ]
    context = f"Classify the minimum effort needed for this coding task. Task: {request[:4000]}\nIt is"
    first = await _score(client, url, context, options, norm="mean")
    second = list(reversed(await _score(client, url, context, list(reversed(options)), norm="mean")))
    first_choice = max(range(len(options)), key=lambda index: first[index][0])
    second_choice = max(range(len(options)), key=lambda index: second[index][0])
    return "small" if first_choice == second_choice == 0 and first[0][1] >= 0.35 else "standard"


async def choose(
    request: str, profile: Profile, candidates: list[Candidate], jev_url: str,
    client: httpx.AsyncClient | None = None,
    policy_band: str | None = None,
    implementation_contract: ImplementationContract | None = None,
) -> Decision:
    if not candidates:
        raise RuntimeError("No requested model and reasoning pair is available")
    band = policy_band or _policy_band(request, profile)
    if (policy_band is None and implementation_contract is not None
            and band in ("small", "standard", "unclassified")
            and _bounded_implementation(implementation_contract, request, profile)):
        band = "bounded_implementation"
    if len(candidates) == 1:
        return Decision(candidates[0], "only_available_pair", None, 1, profile,
                        band)
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=60)
    assert client is not None
    try:
        if band == "unclassified":
            try:
                band = await _classify_unfamiliar_task(client, jev_url, request)
            except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, asyncio.TimeoutError):
                band = "standard"
        eligible = [index for index, candidate in enumerate(candidates) if _eligible(candidate, band)]
        if band == "explicit_astra":
            if not eligible:
                raise RuntimeError("Requested Astra is unavailable")
            preferred = next((index for index in eligible if candidates[index].effort == "medium"), eligible[0])
            return Decision(candidates[preferred], "explicit_astra_request", None, len(candidates), profile, band)
        if not eligible:
            return Decision(fallback(candidates), "policy_no_candidate_fallback", None, len(candidates), profile, band)
        scoring_indices = eligible if band == "bounded_implementation" else list(range(len(candidates)))
        options = [_option(candidates[index]) for index in scoring_indices]
        context = _context(request, profile)
        if band == "bounded_implementation" and implementation_contract is not None:
            context += (
                "\nThe coordinator has resolved design and behavior decisions for a bounded, low-risk "
                "implementation subtask. Choose the least costly model capable of this specific brief.\n"
                f"Requirement: {implementation_contract.requirement[:1000]}\n"
                f"Allowed paths: {', '.join(implementation_contract.allowed_paths)}\n"
                f"Existing patterns: {'; '.join(implementation_contract.existing_patterns)[:1000]}\n"
                f"Acceptance checks: {'; '.join(implementation_contract.acceptance_checks)[:1000]}\n"
            )
        first = await _score(client, jev_url, context, options)
        reversed_scores = await _score(client, jev_url, context, list(reversed(options)))
        second = list(reversed(reversed_scores))
        indexed_scores = [(score_index, index) for score_index, index in enumerate(scoring_indices) if index in eligible]
        first_choice = max(indexed_scores, key=lambda pair: first[pair[0]][0])[1]
        second_choice = max(indexed_scores, key=lambda pair: second[pair[0]][0])[1]
        if first_choice != second_choice:
            return Decision(_policy_fallback([candidates[index] for index in eligible]), "jev_order_disagreement_fallback", None, len(candidates), profile, band)
        score_index = scoring_indices.index(first_choice)
        return Decision(candidates[first_choice], "open_jev_policy_ranked", first[score_index][1], len(candidates), profile, band)
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, asyncio.TimeoutError):
        return Decision(_policy_fallback([candidates[index] for index in eligible]), "jev_unavailable_or_invalid_fallback", None, len(candidates), profile, band)
    finally:
        if own_client:
            await client.aclose()
