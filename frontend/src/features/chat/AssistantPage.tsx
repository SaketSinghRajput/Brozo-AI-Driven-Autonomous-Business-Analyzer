import { useNavigate, useParams } from 'react-router';
import { ChatWorkspace } from './ChatWorkspace';

/**
 * Full-page Assistant view (UI-UX §24). The selected conversation lives in the URL
 * (/assistant/:conversationId) so history entries can reopen it. The docked variant is
 * features/chat/AssistantPanel.tsx.
 */
export function AssistantPage() {
  const { conversationId = null } = useParams<{ conversationId: string }>();
  const navigate = useNavigate();
  return (
    <div className="flex h-full max-w-4xl flex-col gap-4">
      <h1 className="text-h1 text-text">Assistant</h1>
      <ChatWorkspace
        conversationId={conversationId}
        onSelect={(id) => void navigate(`/assistant/${encodeURIComponent(id)}`)}
      />
    </div>
  );
}
