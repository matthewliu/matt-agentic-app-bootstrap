/**
 * Shared TypeScript types mirroring backend Pydantic models.
 * Keep in sync with app/models/ — see AGENTS.md, "Type sync contract", for the 3-place update rule.
 */

export interface Item {
  id: string;
  name: string;
  description?: string;
  created_at: string;
  updated_at: string;
}

export interface UserProfile {
  id: string;
  email: string;
  name?: string;
  picture?: string;
}
