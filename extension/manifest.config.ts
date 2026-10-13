import { defineManifest } from '@crxjs/vite-plugin';

import { hostPermissions, validateApiBase } from './src/api-base.ts';

// The REF server this build talks to (VITE_REF_API_BASE, validated: HTTPS unless loopback).
const apiBase = validateApiBase(process.env['VITE_REF_API_BASE']);

// The content script is injected on every instagram.com page because Instagram navigates without
// reloading, so a script matched only to /reels/* would miss a user arriving from the home feed.
// It is idle everywhere except https://www.instagram.com/reels/* (src/content/controller.ts checks
// the address itself, and the service worker only activates the monitored tab on that surface).
// Host permissions (owner-approved 2026-10-07):
// - instagram.com: lets the service worker see a tab's URL, to end a session when the user leaves
//   the Reels surface for 60 seconds. Without it Chrome hides the URL of the tab.
// - the REF server: 127.0.0.1 and localhost in the default build; a build for a hosted server
//   (VITE_REF_API_BASE=https://...) asks for that HTTPS server only (audit H-4). Extension fetches
//   to hosts it has permission for need no CORS.
export default defineManifest({
  manifest_version: 3,
  name: 'Reel Exposure Fingerprint',
  version: '0.1.0',
  description: 'Research tool. Observes the Reels you choose to watch, with your consent.',
  permissions: ['storage', 'alarms'],
  host_permissions: hostPermissions(apiBase),
  action: {
    default_title: 'Reel Exposure Fingerprint',
    default_popup: 'src/popup/index.html',
  },
  content_scripts: [
    {
      matches: ['https://www.instagram.com/*'],
      js: ['src/content/index.ts'],
      run_at: 'document_idle',
      all_frames: false,
    },
  ],
  background: {
    service_worker: 'src/background/index.ts',
    type: 'module',
  },
});
