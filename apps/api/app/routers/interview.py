"""Card 0 — guided brand-kit interview.

One-question-at-a-time chat flow. ``POST /start`` opens a session with the
first of the seven Card 0 questions pending; ``POST /{id}/answer`` records the
answer and returns the next question (optionally a single LLM-generated
probing follow-up when a live provider is configured); ``POST /{id}/finish``
drafts the first brand-kit version from the answers; ``POST /{id}/confirm``
persists it as a real BrandKit row.

Stubs-first: with ``LLM_PROVIDER=stub`` (the default) no follow-up is ever
generated and the flow is fully deterministic.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import forge_llm
from fastapi import APIRouter, HTTPException, status
from forge_db.models import BrandKit, Business, InterviewSession, InterviewStatus

from app import schemas
from app.core.deps import CurrentUser, DbSession, get_owned_or_404

router = APIRouter(prefix="/interview", tags=["interview"])

# The seven Card 0 questions, in README order.
INTERVIEW_QUESTIONS: list[str] = [
    "What does your business sell, and to whom?",
    "What's the offer you're proudest of?",
    "What are the five marketing tasks you actually do each week?",
    "Where do your customers hang out?",
    "Which three brands' marketing do you admire — and why?",
    "What words or phrases should never appear in your marketing?",
    "Tell me about one campaign that flopped, and what you think went wrong.",
]

TOTAL_QUESTIONS = len(INTERVIEW_QUESTIONS)

BRAND_KIT_FIELDS = (
    "name",
    "voice_description",
    "tone_tags",
    "primary_color",
    "secondary_color",
    "fonts",
    "icp_description",
    "do_list",
    "dont_list",
)


def _as_response(session: InterviewSession) -> dict:
    """Shape the public turn payload from a session row."""
    done = session.current_index >= TOTAL_QUESTIONS
    question = None if done else session.answers[-1]["question"]
    return {
        "session_id": session.id,
        "status": session.status.value,
        "question_index": min(session.current_index, TOTAL_QUESTIONS),
        "total_questions": TOTAL_QUESTIONS,
        "question": question,
        "done": done,
    }


def _require_active(session: InterviewSession) -> None:
    if session.status != InterviewStatus.active:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Interview session is {session.status.value}, not active",
        )


async def _maybe_followup(session: InterviewSession) -> str | None:
    """Generate ONE probing follow-up question when a live LLM is configured.

    Returns ``None`` on the stub provider or on any error — the caller falls
    through to the next main question.
    """
    try:
        provider = forge_llm.get_provider()
    except Exception:
        return None
    if provider.name == "stub":
        return None
    try:
        history = "\n".join(
            f"Q: {entry['question']}\nA: {entry['answer']}"
            for entry in session.answers
            if entry.get("answer")
        )
        result = await provider.generate(
            forge_llm.GenerationRequest(
                prompt=(
                    "You are interviewing a business owner to draft their "
                    "marketing brand kit. Here is the interview so far:\n\n"
                    f"{history}\n\n"
                    "The last answer above is the most recent. Write ONE short, "
                    "specific probing follow-up question that digs into something "
                    "concrete, surprising, or vague in that answer. Reply with ONLY "
                    "the question — no preamble, no quotation marks."
                ),
                system_prompt=(
                    "You are a sharp marketing interviewer. Ask exactly one "
                    "probing follow-up question."
                ),
                max_tokens=120,
                temperature=0.7,
            )
        )
        text = (result.text or "").strip()
        if not text:
            return None
        return text.splitlines()[0].strip().strip("\"'")[:500] or None
    except Exception:
        return None


def _draft_brand_kit(business_name: str, answers: list[dict]) -> dict:
    """Deterministically draft a brand-kit payload from the 7 main answers."""
    mains = [str(entry.get("answer") or "") for entry in answers if not entry.get("followup")]
    while len(mains) < TOTAL_QUESTIONS:
        mains.append("")
    sell_to_whom, proudest_offer, weekly_tasks, _hangouts, admired, never_say, _flop = mains[:7]

    voice = f"Brands admired: {admired}. Weekly marketing rhythm: {weekly_tasks}.".strip()
    dont_list = [
        part.strip()
        for chunk in never_say.replace("\n", ",").split(",")
        for part in [chunk]
        if part.strip()
    ]
    return {
        "name": f"{business_name} — Interview Kit" if business_name else "Interview Kit",
        "voice_description": voice or None,
        "tone_tags": [],
        "primary_color": None,
        "secondary_color": None,
        "fonts": {},
        "icp_description": sell_to_whom or None,
        "do_list": [proudest_offer] if proudest_offer.strip() else [],
        "dont_list": dont_list,
    }


@router.post("/start", response_model=schemas.InterviewQuestionResponse, status_code=201)
def start_interview(user: CurrentUser, db: DbSession):
    """Open a new interview session with the first Card 0 question pending."""
    session = InterviewSession(
        business_id=user.business_id,
        user_id=user.id,
        status=InterviewStatus.active,
        current_index=0,
        answers=[{"question": INTERVIEW_QUESTIONS[0], "answer": None, "followup": False}],
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return _as_response(session)


@router.post("/{session_id}/answer", response_model=schemas.InterviewQuestionResponse)
async def answer_interview(
    session_id: uuid.UUID, payload: schemas.InterviewAnswerRequest, user: CurrentUser, db: DbSession
):
    """Record the answer to the pending question and return the next turn.

    404 when the session is not owned by the caller's business; 422 when the
    session is not active or there is no pending question.
    """
    session = get_owned_or_404(db, InterviewSession, session_id, user)
    _require_active(session)

    answer_text = payload.answer.strip()
    if not answer_text:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Answer must not be blank",
        )

    answers = [dict(entry) for entry in session.answers]
    if not answers or answers[-1].get("answer") is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="No pending question to answer",
        )
    answers[-1]["answer"] = answer_text

    if answers[-1].get("followup"):
        # Follow-up answered — advance past the main question it probed.
        session.current_index += 1
    else:
        followup = await _maybe_followup(session)
        if followup:
            answers.append({"question": followup, "answer": None, "followup": True})
        else:
            session.current_index += 1

    if session.current_index < TOTAL_QUESTIONS and (
        not answers or answers[-1].get("answer") is not None
    ):
        # No pending follow-up: queue the next main question.
        answers.append(
            {
                "question": INTERVIEW_QUESTIONS[session.current_index],
                "answer": None,
                "followup": False,
            }
        )

    session.answers = answers
    db.commit()
    db.refresh(session)
    return _as_response(session)


@router.post("/{session_id}/finish", response_model=schemas.InterviewFinishResponse)
def finish_interview(session_id: uuid.UUID, user: CurrentUser, db: DbSession):
    """Close the interview and draft the first brand-kit version.

    404 when not owned; 422 unless the session is active and all 7 main
    questions are answered.
    """
    session = get_owned_or_404(db, InterviewSession, session_id, user)
    _require_active(session)

    mains = [entry for entry in session.answers if not entry.get("followup")]
    if (
        session.current_index < TOTAL_QUESTIONS
        or len(mains) < TOTAL_QUESTIONS
        or any(entry.get("answer") is None for entry in mains)
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Interview is not complete: answer all 7 questions first",
        )

    business = db.get(Business, user.business_id)
    draft = _draft_brand_kit(business.name if business else "", session.answers)
    session.draft_brand_kit = draft
    session.status = InterviewStatus.completed
    session.completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(session)
    return {
        "session_id": session.id,
        "status": session.status.value,
        "draft_brand_kit": draft,
    }


@router.post("/{session_id}/confirm", response_model=schemas.InterviewConfirmResponse)
def confirm_interview(
    session_id: uuid.UUID,
    payload: schemas.InterviewConfirmRequest,
    user: CurrentUser,
    db: DbSession,
):
    """Persist the drafted kit as a BrandKit row (version 1) and link it.

    404 when not owned; 422 unless the session is completed and not already
    confirmed. Optional ``overrides`` patch individual brand-kit fields.
    """
    session = get_owned_or_404(db, InterviewSession, session_id, user)
    if session.status != InterviewStatus.completed or session.brand_kit_id is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Interview must be completed before confirming, and not already confirmed",
        )

    draft = {k: v for k, v in dict(session.draft_brand_kit or {}).items() if k in BRAND_KIT_FIELDS}
    if payload.overrides is not None:
        draft.update(payload.overrides.model_dump(exclude_unset=True))

    kit = BrandKit(business_id=user.business_id, version=1, **draft)
    db.add(kit)
    db.flush()
    session.brand_kit_id = kit.id
    db.commit()
    db.refresh(kit)
    return {"brand_kit": kit}
