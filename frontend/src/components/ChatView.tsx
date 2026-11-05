import { useEffect, useRef, useState } from "react";

import { NewStoryModal } from "@/components/NewStoryModal";
import { AssistantMessage, UserMessage } from "@/components/MessageItem";
import { ModelPicker } from "@/components/ModelPicker";
import { useStore } from "@/store";
function EmptyState() {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
      <h2 className="text-lg font-medium text-zinc-300">Start the conversation</h2>
      <p className="max-w-md text-sm leading-relaxed text-zinc-500">
        No API keys yet? The <span className="font-mono text-emerald-300">echo</span>{" "}
        provider works out of the box. To talk to real models, copy{" "}
        <span className="font-mono text-zinc-400">backend/.env.example</span> to{" "}
        <span className="font-mono text-zinc-400">backend/.env</span>, add a key, and
        restart the server.
      </p>
    </div>
  );
}

function Composer() {
  const streaming = useStore((s) => s.streaming);
  const send = useStore((s) => s.send);
  const stop = useStore((s) => s.stop);
  const [text, setText] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);

  const resize = () => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  };

  const submit = () => {
    const content = text.trim();
    if (!content || streaming) return;
    setText("");
    requestAnimationFrame(resize);
    send(content);
  };

  return (
    <div className="border-t border-zinc-800/70 bg-zinc-950/80 px-4 py-4">
      <div className="mx-auto flex w-full max-w-3xl items-end gap-2">
        <textarea
          ref={ref}
          rows={1}
          value={text}
          placeholder="Message… (Enter to send, Shift+Enter for newline)"
          onChange={(e) => {
            setText(e.target.value);
            resize();
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          className="max-h-50 flex-1 resize-none rounded-xl border border-zinc-700 bg-zinc-900 px-3.5 py-2.5 text-[15px] text-zinc-100 placeholder-zinc-500 outline-none focus:border-zinc-500"
        />
        {streaming ? (
          <button
            onClick={stop}
            className="h-10 shrink-0 rounded-xl bg-red-900/60 px-4 text-sm font-medium text-red-200 transition hover:bg-red-900/80"
          >
            Stop
          </button>
        ) : (
          <button
            onClick={submit}
            disabled={!text.trim()}
            className="h-10 shrink-0 rounded-xl bg-zinc-100 px-4 text-sm font-medium text-zinc-900 transition hover:bg-white disabled:cursor-not-allowed disabled:opacity-30"
          >
            Send
          </button>
        )}
      </div>
    </div>
  );
}

export function ChatView() {
  const messages = useStore((s) => s.messages);
  const streaming = useStore((s) => s.streaming);
  const streamText = useStore((s) => s.streamText);
  const streamTools = useStore((s) => s.streamTools);
  const streamError = useStore((s) => s.streamError);
  const sessions = useStore((s) => s.sessions);
  const currentId = useStore((s) => s.currentId);
  const bottom = useRef<HTMLDivElement>(null);
  const [storyModal, setStoryModal] = useState(false);

  const title = sessions.find((s) => s.id === currentId)?.title;

  // seed a new story from this conversation: last user ask + model's answer
  const storyInitial = () => {
    const lastUser = [...messages].reverse().find((m) => m.role === "user");
    const lastAssistant = [...messages].reverse().find((m) => m.role === "assistant");
    if (!lastUser && !lastAssistant) return {};
    const description = [
      lastUser ? `Request:\n${lastUser.content}` : "",
      lastAssistant ? `\nModel reply:\n${lastAssistant.content.slice(0, 2000)}` : "",
    ]
      .filter(Boolean)
      .join("\n");
    return { title: title && title !== "New chat" ? title : undefined, description };
  };

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [messages.length, streamText, streamError]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex h-12 shrink-0 items-center justify-between gap-4 border-b border-zinc-800/70 px-4">
        <h1 className="min-w-0 truncate text-sm font-medium text-zinc-300">
          {title ?? "Fubala"}
        </h1>
        <div className="flex shrink-0 items-center gap-2">
          <button
            onClick={() => setStoryModal(true)}
            disabled={messages.length === 0}
            title="Turn this conversation into a story on the board"
            className="rounded-lg border border-zinc-700 px-2.5 py-1.5 text-xs text-zinc-300 transition hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-30"
          >
            ＋ Story
          </button>
          <ModelPicker />
        </div>
      </header>

      {streamError && (
        <div className="border-b border-red-900/50 bg-red-950/40 px-4 py-2 text-center text-xs text-red-300">
          {streamError}
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-6">
        <div className="mx-auto flex w-full max-w-3xl flex-col gap-5">
          {messages.length === 0 && !streaming ? (
            <EmptyState />
          ) : (
            messages.map((m) =>
              m.role === "user" ? (
                <UserMessage key={m.id} message={m} />
              ) : (
                <AssistantMessage key={m.id} message={m} />
              ),
            )
          )}
          {streaming && (
            <div>
              {streamTools.length > 0 && (
                <div className="mb-2 flex flex-wrap gap-1.5">
                  {streamTools.map((t, i) => (
                    <span
                      key={i}
                      className="rounded-full border border-sky-900/60 bg-sky-950/40 px-2 py-0.5 font-mono text-[10px] text-sky-300"
                      title={t.arguments}
                    >
                      🔧 {t.name}({t.arguments.length > 24 ? t.arguments.slice(0, 24) + "…" : t.arguments})
                    </span>
                  ))}
                </div>
              )}
              <AssistantMessage
                streaming
                message={{
                  id: -1,
                  content: streamText || " ",
                  model: null,
                  prompt_tokens: null,
                  completion_tokens: null,
                  finish_reason: null,
                }}
              />
            </div>
          )}
          <div ref={bottom} />
        </div>
      </div>

      <Composer />

      {storyModal && (
        <NewStoryModal onClose={() => setStoryModal(false)} initial={storyInitial()} />
      )}
    </div>
  );
}
