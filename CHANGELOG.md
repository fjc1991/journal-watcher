# Changelog

Release notes: `.github/workflows/release.yml` reads the bullets under the heading of the version being released (`## 1.2.0`) and shows them in the app's "Update available" dialog.

## 1.1.0

- Sign-ins show what state they are in: a green tick and "Signed in · 3 h ago" for each one that worked, a "waiting for you" row while a window is open, and a green dot on the Sign-ins button. The browser window itself shows a green tick before it closes.
- Library access for any university: Settings → Library access (OpenAthens, EZproxy, or each publisher's own "sign in through your institution" page). No institution is built in any more.
- The Sign-ins menu stays usable while a sign-in window is open.
- Sign-in problems now say why (for example when the app's browser could not start) instead of "wasn't completed".
- Open-source repository layout: your data stays in your data folder; starting settings come from `defaults/`.

## 1.0.0

- First public release: weekly journal watch with AI screening, a library, a PDF reader with highlights, notes and Ask, folders with matrix / ideas / related-work tools, flashcards.
- Light-first design with a sidebar; a lighter dark theme.
- Mac app with a drag-to-Applications installer and update notices ("Update now").
- A welcome guide for first-time users: AI keys and sign-ins explained.
- Library access for any university (OpenAthens, EZproxy, or each publisher's institution login), with a green tick for every sign-in that worked.
