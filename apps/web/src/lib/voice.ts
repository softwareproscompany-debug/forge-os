import { useCallback, useEffect, useRef, useState } from "react";

type SpeechRecognitionT = typeof window extends never
  ? never
  : any;

function getRecognition(): SpeechRecognitionT | null {
  if (typeof window === "undefined") return null;
  const w = window as any;
  const Ctor = w.SpeechRecognition || w.webkitSpeechRecognition;
  return Ctor ? new Ctor() : null;
}

export function ttsSupported(): boolean {
  return typeof window !== "undefined" && "speechSynthesis" in window;
}

export function sttSupported(): boolean {
  return getRecognition() !== null;
}

export interface UseVoiceOpts {
  /** Called with the final transcript when the user stops speaking. */
  onResult: (text: string) => void;
  /** When true, the mic re-arms automatically after each TTS reply. */
  conversationMode: boolean;
  speakReplies: boolean;
  /** Speaking rate for TTS replies (0.7–1.3). Defaults to 1.02. */
  rate?: number;
}

/**
 * Jarvis-style voice loop: mic → transcript → (caller fetches reply) →
 * spoken aloud → mic re-arms. Works with the Web Speech API built into
 * Chrome, Edge, Safari (iOS/iPadOS 14.5+) — no keys, no servers.
 */
export function useVoice({ onResult, conversationMode, speakReplies, rate = 1.02 }: UseVoiceOpts) {
  const [listening, setListening] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const [interim, setInterim] = useState("");
  const [error, setError] = useState<string | null>(null);
  const recRef = useRef<SpeechRecognitionT | null>(null);
  const wantListen = useRef(false);
  const optsRef = useRef({ onResult, conversationMode, speakReplies, rate });
  optsRef.current = { onResult, conversationMode, speakReplies, rate };
  /** Live mic capture stream, kept alive while listening so the orb can
   *  analyze real input audio. Null when not listening. */
  const [micStream, setMicStream] = useState<MediaStream | null>(null);
  const micStreamRef = useRef<MediaStream | null>(null);

  const releaseMicStream = useCallback(() => {
    micStreamRef.current?.getTracks().forEach((t) => {
      try { t.stop(); } catch { /* noop */ }
    });
    micStreamRef.current = null;
    setMicStream(null);
  }, []);

  const stopSpeaking = useCallback(() => {
    if (ttsSupported()) {
      window.speechSynthesis.cancel();
      setSpeaking(false);
    }
  }, []);

  const startListening = useCallback(async () => {
    const rec = getRecognition();
    if (!rec) {
      setError("Voice input isn't supported in this browser — type instead.");
      return;
    }
    // Android Chrome will silently fail to start SpeechRecognition unless the
    // page already holds microphone permission. Prime it explicitly first so
    // the user gets the system permission prompt instead of silence.
    // The stream is kept alive while listening so the orb can analyze the
    // real input signal; it is released when listening stops.
    try {
      if (navigator.mediaDevices?.getUserMedia) {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        releaseMicStream();
        micStreamRef.current = stream;
        setMicStream(stream);
      }
    } catch {
      setError(
        "Microphone access was blocked. Allow the microphone for this site in your browser settings, then tap the mic again."
      );
      return;
    }
    try {
      recRef.current?.abort();
    } catch {
      /* noop */
    }
    const r = getRecognition()!;
    r.lang = "en-US";
    r.interimResults = true;
    r.maxAlternatives = 1;
    r.continuous = false;

    let finalText = "";
    r.onresult = (e: any) => {
      let interimText = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const t = e.results[i][0].transcript;
        if (e.results[i].isFinal) finalText += t;
        else interimText += t;
      }
      setInterim(interimText);
      if (finalText) {
        optsRef.current.onResult(finalText.trim());
        finalText = "";
        setInterim("");
      }
    };
    r.onerror = (e: any) => {
      const code = e?.error;
      if (code === "not-allowed" || code === "service-not-allowed") {
        setError(
          "Microphone access was blocked. Allow the microphone for this site in your browser settings, then tap the mic again."
        );
      } else if (code === "audio-capture") {
        setError("No microphone found on this device — you can still type below.");
      } else if (code === "network") {
        setError("Couldn't reach the speech service — check your connection and try again.");
      }
      // 'no-speech' and 'aborted' are not errors worth surfacing.
      setListening(false);
    };
    r.onend = () => {
      setListening(false);
      // Re-arm for back-and-forth when the user wants conversation mode
      // and we're not about to speak a reply (speak() re-arms on end).
      if (wantListen.current && optsRef.current.conversationMode && !optsRef.current.speakReplies) {
        startListening();
      } else {
        releaseMicStream();
      }
    };
    try {
      r.start();
      recRef.current = r;
      wantListen.current = true;
      setListening(true);
      setError(null);
    } catch {
      setError("Could not start the microphone — try again.");
    }
  }, [releaseMicStream]);

  const stopListening = useCallback(() => {
    wantListen.current = false;
    try {
      recRef.current?.stop();
    } catch {
      /* noop */
    }
    releaseMicStream();
    setListening(false);
    setInterim("");
  }, [releaseMicStream]);

  /** Speak text aloud; re-arms the mic when done in conversation mode. */
  const speak = useCallback(
    (text: string) => {
      if (!ttsSupported() || !optsRef.current.speakReplies) return;
      // Pause capture while speaking, but keep the resume intent so the mic
      // re-arms for back-and-forth when the reply finishes.
      const resume = wantListen.current && optsRef.current.conversationMode;
      try {
        recRef.current?.abort();
      } catch {
        /* noop */
      }
      // Mic goes quiet while Draven speaks — re-armed on utterance end.
      releaseMicStream();
      setListening(false);
      setInterim("");
      const u = new SpeechSynthesisUtterance(text);
      u.rate = optsRef.current.rate ?? 1.02;
      u.pitch = 0.9;
      // Prefer a natural/en voice when the platform offers one.
      const voices = window.speechSynthesis.getVoices();
      const pick =
        voices.find((v) => /en[-_]US/i.test(v.lang) && /natural|neural|samantha|google us english/i.test(v.name)) ||
        voices.find((v) => /^en/i.test(v.lang));
      if (pick) u.voice = pick;
      u.onstart = () => setSpeaking(true);
      u.onend = () => {
        setSpeaking(false);
        if (resume) {
          // Small beat so the mic doesn't catch the tail of our own voice.
          window.setTimeout(() => {
            if (wantListen.current) startListening();
          }, 350);
        } else {
          wantListen.current = false;
        }
      };
      u.onerror = () => setSpeaking(false);
      window.speechSynthesis.cancel();
      window.speechSynthesis.speak(u);
    },
    [startListening, stopListening, releaseMicStream]
  );

  // Load voices (some platforms populate async).
  useEffect(() => {
    if (!ttsSupported()) return;
    window.speechSynthesis.getVoices();
    const h = () => window.speechSynthesis.getVoices();
    window.speechSynthesis.addEventListener?.("voiceschanged", h);
    return () => window.speechSynthesis.removeEventListener?.("voiceschanged", h);
  }, []);

  // Cleanup on unmount.
  useEffect(() => {
    return () => {
      wantListen.current = false;
      try {
        recRef.current?.abort();
      } catch {
        /* noop */
      }
      if (ttsSupported()) window.speechSynthesis.cancel();
      releaseMicStream();
    };
  }, [releaseMicStream]);

  return {
    listening,
    speaking,
    interim,
    error,
    micStream,
    startListening,
    stopListening,
    speak,
    stopSpeaking,
    stt: sttSupported(),
    tts: ttsSupported(),
  };
}
