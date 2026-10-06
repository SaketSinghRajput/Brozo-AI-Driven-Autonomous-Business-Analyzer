import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { unwrap } from '../../api/errors';
import {
  createConversationApiV1ChatConversationsPost,
  listConversationsApiV1ChatConversationsGet,
  listMessagesApiV1ChatConversationsConversationIdMessagesGet,
} from '../../api/generated/sdk.gen';
import type { ConversationCreate } from '../../api/generated/types.gen';

export const chatKeys = {
  conversations: ['conversations'] as const,
  messages: (conversationId: string) => ['conversations', conversationId, 'messages'] as const,
};

export function useConversations() {
  return useQuery({
    queryKey: chatKeys.conversations,
    queryFn: () => unwrap(listConversationsApiV1ChatConversationsGet()),
  });
}

/** Stored messages of a conversation (history / reopen). Messages are append-only. */
export function useMessages(conversationId: string) {
  return useQuery({
    queryKey: chatKeys.messages(conversationId),
    queryFn: () =>
      unwrap(
        listMessagesApiV1ChatConversationsConversationIdMessagesGet({
          path: { conversation_id: conversationId },
        }),
      ),
  });
}

export function useCreateConversation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: ConversationCreate) =>
      unwrap(createConversationApiV1ChatConversationsPost({ body })),
    onSuccess: () => client.invalidateQueries({ queryKey: chatKeys.conversations }),
  });
}
