import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";

import type { Message } from "@/types";

export function Markdown({ content }: { content: string }) {
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
        {content}
      </ReactMarkdown>
    </div>
  );
}

function Meta({ message }: { message: Message }) {
  const bits: string[] = [];
  if (message.model) bits.push(message.model);
  if (message.prompt_tokens != null || message.completion_tokens != null) {
    bits.push(`${message.prompt_tokens ?? "?"}→${message.completion_tokens ?? "?"} tok`);
  }
  if (message.finish_reason) bits.push(message.finish_reason);
  if (bits.length === 0) return null;
  const bad = message.finish_reason === "error";
  return (
    <p className={`mt-1.5 text-[11px] ${bad ? "text-red-400" : "text-zinc-500"}`}>
      {bits.join(" · ")}
    </p>
  );
}

export function UserMessage({ message }: { message: Message }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[85%]">
        <div className="rounded-2xl rounded-br-md bg-zinc-800 px-4 py-2.5 text-[15px] whitespace-pre-wrap text-zinc-100">
          {message.content}
        </div>
      </div>
    </div>
  );
}

export function AssistantMessage({
  message,
  streaming = false,
}: {
  message: Pick<Message, "content" | "model" | "prompt_tokens" | "completion_tokens" | "finish_reason"> & {
    id: number;
  };
  streaming?: boolean;
}) {
  return (
    <div className="max-w-full">
      <div className={streaming ? "stream-cursor" : undefined}>
        <Markdown content={message.content} />
      </div>
      {!streaming && (
        <Meta
          message={{
            id: message.id,
            session_id: "",
            role: "assistant",
            created_at: "",
            content: message.content,
            model: message.model,
            prompt_tokens: message.prompt_tokens,
            completion_tokens: message.completion_tokens,
            finish_reason: message.finish_reason,
          }}
        />
      )}
    </div>
  );
}
