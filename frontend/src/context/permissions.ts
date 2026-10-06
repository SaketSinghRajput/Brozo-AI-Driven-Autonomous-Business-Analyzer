import type { Role } from '../api/generated/types.gen';

/**
 * UI-side mirror of backend/app/security/permissions.py ROLE_PERMISSIONS, used only to hide or
 * disable actions a role lacks (UI-UX §43). The backend enforces every permission itself (403);
 * this table never grants anything. Keep it in sync with the backend matrix.
 */
export type Permission =
  | 'VIEW_DATASET'
  | 'VIEW_QUERY'
  | 'UPLOAD_DATASET'
  | 'EDIT_SEMANTICS'
  | 'RUN_ANALYSIS'
  | 'TRAIN_MODEL'
  | 'ACCESS_DOCUMENTS'
  | 'READ_MEMORY'
  | 'WRITE_MEMORY'
  | 'RUN_SIMULATION'
  | 'EXECUTE_TOOL'
  | 'RUN_REPAIR'
  | 'APPROVE_REPAIR'
  | 'DEPLOY_MODEL'
  | 'MANAGE_USERS'
  | 'VIEW_AUDIT';

const VIEWER: Permission[] = [
  'VIEW_DATASET',
  'RUN_ANALYSIS',
  'EXECUTE_TOOL',
  'ACCESS_DOCUMENTS',
  'READ_MEMORY',
  'RUN_SIMULATION',
];
const ANALYST: Permission[] = [
  ...VIEWER,
  'VIEW_QUERY',
  'UPLOAD_DATASET',
  'EDIT_SEMANTICS',
  'TRAIN_MODEL',
  'WRITE_MEMORY',
];
const ADMIN_ONLY: Permission[] = [
  'RUN_REPAIR',
  'APPROVE_REPAIR',
  'DEPLOY_MODEL',
  'MANAGE_USERS',
  'VIEW_AUDIT',
];

const ROLE_PERMISSIONS: Record<Role, ReadonlySet<Permission>> = {
  viewer: new Set(VIEWER),
  analyst: new Set(ANALYST),
  admin: new Set([...ANALYST, ...ADMIN_ONLY]),
};

export function roleHas(role: Role, permission: Permission): boolean {
  return ROLE_PERMISSIONS[role].has(permission);
}

export const ROLE_LABELS: Record<Role, string> = {
  viewer: 'Viewer',
  analyst: 'Analyst',
  admin: 'Administrator',
};

/** Minimum role naming for a disabled action's reason text (UI-UX §43). */
export function requiredRoleText(permission: Permission): string {
  if (roleHas('analyst', permission)) return 'Requires Analyst role';
  return 'Requires administrator permission';
}
