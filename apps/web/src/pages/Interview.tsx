import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { Link } from "react-router-dom";
import { interviewApi } from "../lib/api";
import type {
  BrandKitOverrides,
  DraftBrandKit,
  InterviewQuestionState,
} from "../lib/api";
import { linesToList, listToLines } from "../lib/format";
import {
  Badge,
  ErrorBanner,
  Field,
  PageHeader,
  Spinner,
} from "../components/ui";

type Phase = "intro" | "chat" | "review" | "done";

interface ChatMessage {
  id: number;
  role: "bot" | "user";
  text: string;
}

const INTRO_BLURB =
  "Answer seven questions about your business — one at a time. " +
  "When you're done, ForgeOS drafts the first version of your brand kit " +
  "from your answers. You review it, tweak it, and save it.";

export default function InterviewPage() {
  const [phase, setPhase] = useState<Phase>("intro");
  const [starting, setStarting] = useState(false);
  const [sending, setSending] = useState(false);
  const [finishing, setFinishing] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const [sessionId, setSessionId] = useState<string | null>(null);
  const [turn, setTurn] = useState<InterviewQuestionState | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");

  // Draft brand-kit review fields.
  const [draft, setDraft] = useState<DraftBrandKit | null>(null);
  const [kitName, setKitName] = useState("");
  const [voice, setVoice] = useState("");
  const [tone, setTone] = useState("");
  const [icp, setIcp] = useState("");
  const [doList, setDoList] = useState("");
  const [dontList, setDontList] = useState("");

  const idRef = useRef(0);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, phase]);

  const pushMessage = (role: "bot" | "user", text: string): number => {
    idRef.current += 1;
    const id = idRef.current;
    setMessages((prev) => [...prev, { id, role, text }]);
    return id;
  };

  const start = async () => {
    setStarting(true);
    setError(null);
    try {
      const state = await interviewApi.start();
      setSessionId(state.session_id);
      setTurn(state);
      setMessages([]);
      if (state.question) pushMessage("bot", state.question);
      setPhase("chat");
    } catch (err) {
      setError(err);
    } finally {
      setStarting(false);
    }
  };

  const send = async () => {
    const text = input.trim();
    if (!text || sending || !sessionId) return;
    setSending(true);
    setError(null);
    const optimisticId = pushMessage("user", text);
    setInput("");
    try {
      const next = await interviewApi.answer(sessionId, text);
      setTurn(next);
      if (next.question) pushMessage("bot", next.question);
      if (next.done) await finishInterview(sessionId);
    } catch (err) {
      // Roll back the optimistic bubble so a retry doesn't duplicate it.
      setMessages((prev) => prev.filter((m) => m.id !== optimisticId));
      setInput(text);
      setError(err);
    } finally {
      setSending(false);
    }
  };

  const finishInterview = async (sid: string) => {
    setFinishing(true);
    try {
      const fin = await interviewApi.finish(sid);
      const d = fin.draft_brand_kit;
      setDraft(d);
      setKitName(d.name);
      setVoice(d.voice_description ?? "");
      setTone((d.tone_tags ?? []).join(", "));
      setIcp(d.icp_description ?? "");
      setDoList(listToLines(d.do_list));
      setDontList(listToLines(d.dont_list));
      pushMessage(
        "bot",
        "That's everything — I've drafted your first brand kit from your answers. Review it below and save it when it sounds like you.",
      );
      setPhase("review");
    } catch (err) {
      setError(err);
    } finally {
      setFinishing(false);
    }
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  };

  const saveKit = async () => {
    if (!sessionId || !draft) return;
    setSending(true);
    setError(null);
    const overrides: BrandKitOverrides = {
      name: kitName.trim() || draft.name,
      voice_description: voice.trim() || null,
      tone_tags: tone
        .split(",")
        .map((t) => t.trim())
        .filter(Boolean),
      icp_description: icp.trim() || null,
      do_list: linesToList(doList),
      dont_list: linesToList(dontList),
    };
    try {
      await interviewApi.confirm(sessionId, overrides);
      setPhase("done");
    } catch (err) {
      setError(err);
    } finally {
      setSending(false);
    }
  };

  const questionLabel =
    turn && !turn.done
      ? `Question ${turn.question_index + 1} of ${turn.total_questions}`
      : turn?.done
        ? "Complete"
        : "";

  return (
    <div className="interview">
      <PageHeader
        title="Brand Interview"
        subtitle="Card 0 — seven questions, one at a time, then a brand-kit draft"
        actions={
          phase === "chat" && turn ? (
            <span className="badge badge-teal">{questionLabel}</span>
          ) : undefined
        }
      />
      <ErrorBanner error={error} />

      {phase === "intro" && (
        <div className="card interview-intro">
          <h2>Let ForgeOS interview you first</h2>
          <p className="muted">{INTRO_BLURB}</p>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => void start()}
            disabled={starting}
          >
            {starting ? "Starting…" : "Start the interview ❝"}
          </button>
        </div>
      )}

      {(phase === "chat" || phase === "review") && (
        <div className="chat">
          <div className="chat-history" aria-live="polite">
            {messages.map((m) => (
              <div
                key={m.id}
                className={`chat-bubble chat-${m.role === "bot" ? "bot" : "user"}`}
              >
                {m.text}
              </div>
            ))}
            {finishing && (
              <div className="chat-bubble chat-bot">
                <Spinner label="Drafting your brand kit…" />
              </div>
            )}
            <div ref={bottomRef} />
          </div>

          {phase === "chat" && !turn?.done && (
            <div className="chat-input-row">
              <textarea
                className="chat-input"
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={onKeyDown}
                placeholder="Type your answer… (Enter to send, Shift+Enter for a new line)"
                rows={3}
                disabled={sending || finishing}
                aria-label="Your answer"
              />
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => void send()}
                disabled={sending || finishing || !input.trim()}
              >
                {sending ? "Sending…" : "Send"}
              </button>
            </div>
          )}
        </div>
      )}

      {phase === "review" && draft && (
        <div className="card interview-review">
          <h2>Review your brand-kit draft</h2>
          <p className="muted">
            This is version 1, drafted from your answers. Edit anything, then
            save it as your brand kit.
          </p>
          <Field label="Kit name">
            <input
              type="text"
              value={kitName}
              onChange={(e) => setKitName(e.target.value)}
              maxLength={255}
            />
          </Field>
          <Field label="Voice description" hint="How the brand sounds, in your own words">
            <textarea
              value={voice}
              onChange={(e) => setVoice(e.target.value)}
              rows={3}
            />
          </Field>
          <Field label="Tone tags" hint="Comma-separated, e.g. bold, playful">
            <input
              type="text"
              value={tone}
              onChange={(e) => setTone(e.target.value)}
              placeholder="bold, playful"
            />
          </Field>
          <Field label="Audience (ICP)" hint="Who you sell to">
            <textarea value={icp} onChange={(e) => setIcp(e.target.value)} rows={3} />
          </Field>
          <Field label="Do — offers and angles to lean into" hint="One per line">
            <textarea
              value={doList}
              onChange={(e) => setDoList(e.target.value)}
              rows={2}
            />
          </Field>
          <Field label="Don't — words and phrases to never use" hint="One per line">
            <textarea
              value={dontList}
              onChange={(e) => setDontList(e.target.value)}
              rows={2}
            />
          </Field>
          <div className="form-actions">
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => void saveKit()}
              disabled={sending}
            >
              {sending ? "Saving…" : "Save brand kit"}
            </button>
          </div>
        </div>
      )}

      {phase === "done" && (
        <div className="card interview-done">
          <Badge value="completed" />
          <h2>Brand kit saved</h2>
          <p className="muted">
            Your interview is complete and the brand kit is live. Every prompt
            in ForgeOS now renders through it.
          </p>
          <Link to="/onboarding" className="btn btn-primary">
            Open onboarding ⚙
          </Link>
        </div>
      )}
    </div>
  );
}
