from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool

from backend.app.api.deps import ProjectDep
from backend.app.core.config import settings
from backend.app.schemas.assistant import AssistantCommand, AssistantMessageRequest, AssistantMessageResponse, SettingsCommand
from backend.app.services.assistant_context import build_plan_facts
from backend.app.services.assistant_rules import answer_with_rules
from backend.app.services.assistant_service import (
    AssistantNotConfiguredError,
    AssistantUpstreamError,
    ask_yuna,
    default_spacing,
)
from backend.app.services.pipeline_service import ensure_materialized

logger = logging.getLogger(__name__)

# Nested under /api/projects/{project_id}: when the request names a plan_id,
# Юна gets a compact summary of that plan (counts, schemes, area budget) and,
# for one selected item, its normative justification -- see
# assistant_context.py. ProjectDep also gives a proper 404 for a stale project
# id instead of a confusing LLM error.
router = APIRouter(prefix="/api/projects/{project_id}", tags=["assistant"])


def _response(reply: str, actions: list[AssistantCommand], mode: str, notice: str | None = None) -> AssistantMessageResponse:
    legacy = next((a.settings for a in actions if isinstance(a, SettingsCommand)), None)
    return AssistantMessageResponse(reply=reply, actions=actions, action=legacy, mode=mode, notice=notice)


@router.post("/assistant/message", response_model=AssistantMessageResponse)
async def send_message(request: AssistantMessageRequest, project: ProjectDep) -> AssistantMessageResponse:
    facts = None
    if request.plan_id:
        plan = next((p for p in project.plans if p.id == request.plan_id), None)
        if plan is not None and not plan.materialized:
            # The plan on screen can be pruned under the chat's feet: a generate
            # from another tab collapses every other unedited plan
            # (pipeline_service._prune_stale_plans), and facts built from a plan
            # with no items said «деревьев в плане нет». Same transparent
            # recompute get_plan_or_404 does for every other plan route; a
            # failure leaves the facts without counts rather than the chat
            # without an answer.
            try:
                await ensure_materialized(plan)
            except Exception:
                logger.exception("Could not materialize plan %s for the assistant", plan.id)
        facts = await run_in_threadpool(build_plan_facts, project, request.plan_id, request.selected_item_id)
    defaults = default_spacing()

    # No model is not "Юна is off" any more: reviewers build the image straight
    # from the repo with no key, so the built-in parser answers instead. A
    # model that is configured but unreachable (rate limit, network) falls
    # back to the same parser, with a notice saying why.
    notice = None
    if settings.llm_configured:
        try:
            reply, actions = await ask_yuna(
                request.message,
                request.history,
                request.settings,
                facts,
                selection=request.selection,
                violations_count=request.violations_count,
                view=request.view,
                default_spacing_m=defaults,
            )
            return _response(reply, actions, "llm")
        except AssistantNotConfiguredError:
            pass
        except AssistantUpstreamError as exc:
            notice = f"ИИ сейчас недоступен ({exc.reason}) — ответила в базовом режиме"
        except Exception:
            # A bug in our own handling of the model's answer must not leave
            # the user with no answer at all in the middle of a demo.
            logger.exception("Assistant LLM path failed unexpectedly")
            notice = "ИИ сейчас недоступен (внутренняя ошибка) — ответила в базовом режиме"

    reply, actions = answer_with_rules(
        request.message,
        request.settings,
        facts,
        request.selection,
        request.violations_count,
        request.view,
        defaults,
    )
    return _response(reply, actions, "rules", notice)
