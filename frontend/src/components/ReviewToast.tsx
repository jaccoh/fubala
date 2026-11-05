import { useEffect, useState } from "react";

interface ReviewToastProps {
  storyId: string;
  title: string;
  onDismiss: () => void;
  onClick: (storyId: string) => void;
}

export function ReviewToast({ storyId, title, onDismiss, onClick }: ReviewToastProps) {
  const [visible, setVisible] = useState(true);

  useEffect(() => {
    // Auto-dismiss after ~6 seconds
    const timer = setTimeout(() => {
      setVisible(false);
      onDismiss();
    }, 6000);

    return () => clearTimeout(timer);
  }, [onDismiss]);

  if (!visible) return null;

  return (
    <div className="fixed bottom-5 right-5 z-50 flex w-80 items-start gap-3 rounded-lg border border-violet-500/40 bg-violet-950/90 px-4 py-3 shadow-lg backdrop-blur-sm animate-in slide-in-from-bottom-2 fade-in duration-300">
      {/* Icon */}
      <div className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-violet-500/20">
        <span className="text-xs font-medium text-violet-300">👁</span>
      </div>

      {/* Content */}
      <div className="min-w-0 flex-1">
        <p className="text-xs font-semibold text-violet-100">Review needed</p>
        <p className="mt-0.5 truncate text-xs text-violet-200">{title}</p>
      </div>

      {/* Close button */}
      <button
        onClick={(e) => {
          e.stopPropagation();
          setVisible(false);
          onDismiss();
        }}
        className="shrink-0 rounded p-0.5 text-violet-400 transition hover:bg-violet-800/50 hover:text-violet-100"
        aria-label="Dismiss"
      >
        <svg
          className="h-4 w-4"
          fill="none"
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth="2"
          viewBox="0 0 24 24"
          stroke="currentColor"
        >
          <path d="M6 18L18 6M6 6l12 12" />
        </svg>
      </button>

      {/* Clickable overlay */}
      <button
        onClick={() => onClick(storyId)}
        className="absolute inset-0 rounded-lg"
        aria-label={`Open ${title}`}
      />
    </div>
  );
}
