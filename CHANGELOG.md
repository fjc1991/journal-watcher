# Changelog

Release notes: `.github/workflows/release.yml` reads the bullets under the heading of the version being released (`## 1.2.0`) and shows them in the app's "Update available" dialog.

## 1.2.0

- An About page (sidebar → About): who makes Journal Watcher, with the author's university and links, what the app is and how it is licensed, and a way to chip in for a coffee.
- A small reminder card that Journal Watcher is free and takes tips, after 14 days and 5 visits, at most three times and weeks apart. It never appears while you read, while the AI works or over Settings; "Don't ask again" and a switch under About end it. The app only counts, on your own computer, how often you open it: nothing is sent anywhere.
- Update notices can be verified against the author's digital signature (Settings → About & updates says whether this copy does): a feed without a good signature is ignored, and nothing is installed from it.
- Fixed: in the reader, the page number showed the page above after you jumped to a page, and zooming (or Fit, or going back after following a link) moved the text you were reading by about a hundred pixels each time. The text now stays where it was, in the collapsed view too (also when the line you are reading falls on a "lines hidden" bar).
- Fixed: a cell you had started editing in the comparison matrix was lost when a fill finished. Cells now wait until the fill is done.
- Fixed: a download could store a different paper's PDF under the paper's name (for example when a publisher's page linked a cited paper's PDF). Every download is now checked against the paper: its DOI, or its title and authors, must be on the first pages of the PDF. A PDF that is another work is refused (the paper stays in the download queue as “Wrong PDF refused”, with the reason on hover, and no summary is paid for) and the other routes are tried. A PDF the app cannot read, such as a scan, is kept and marked “Identity not checked”. Files already in your library are not rechecked.

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
