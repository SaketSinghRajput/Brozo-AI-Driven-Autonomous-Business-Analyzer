import { useState } from 'react';
import { ChatWorkspace } from './ChatWorkspace';

/** Docked assistant panel (UI-UX §10, §24): the same chat as /assistant, selection kept locally. */
export function AssistantPanel() {
  const [conversationId, setConversationId] = useState<string | null>(null);
  return (
    <aside
      aria-label="Assistant"
      className="flex w-[400px] shrink-0 flex-col gap-3 overflow-y-auto border-l border-border bg-surface p-4"
    >
      <h2 className="text-h3 text-text">Assistant</h2>
      <ChatWorkspace conversationId={conversationId} onSelect={setConversationId} />
    </aside>
  );
}
