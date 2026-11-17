/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** API base path or URL (default "/api/v1"). Never a token. */
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
