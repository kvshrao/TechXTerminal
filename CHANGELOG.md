# Changes

## Unreleased

Highlights

- **Local shells**: PowerShell, the Command Prompt, WSL or Git Bash in a tab, from the shell button,
  the + menu, quick connect (`local`, `local://cmd.exe`) or the command line. A saved session can be
  one too: choose "Local shell" and name the program, and the folder it starts in. Open shells come back
  with "Reopen tabs at start".
- **Scheduled tasks**: run a series of commands on a saved SSH session or on this computer, every so
  many minutes, every day at a time or by hand, and keep what they print in a text file. The
  application runs them while it is open, or Windows Task Scheduler does, also while it is closed.
  Each can be run from the command palette, the task list shows how each last went, an output file
  that every run is added to drops its oldest runs past 10 MB, and tasks are part of backups. A task
  can run its lines as one script, so a `cd` holds for the lines after it, and a run that fails can
  bring a desktop notification, also when Windows ran it.
- **Task chains**: a task can name another to run when every command of it succeeded ("Then run" in the
  task's window), from the application, from Windows Task Scheduler and from `--run-task`. A failed task
  starts nothing, and tasks that name each other run once each.
- **Saved commands that ask**: `{?label}` or `{?label=default}` in a saved command opens a small form for
  the values each time it is sent (a label used twice is asked once). A line break in an answer becomes a
  space, so one answer can never become a second command.
- **Save Selection as Saved Command**: in the terminal's right-click menu, keeps the selected text as a saved command.
- **Approved Host Keys**: in the import menu and the command palette, lists the host keys that were approved,
  with their fingerprints and a filter, and forgets chosen hosts so they are asked about again at the next
  login (for a server whose key changed on purpose). Other lines of the file are left as they are.
- **Command line**: `--tasks` lists the scheduled tasks and `--run-task NAME` runs one without
  opening the window.
- **Encrypted backups**: Back Up Everything asks for a passphrase and encrypts the backup with AES-256-GCM
  (the key made from the passphrase with scrypt), so it is safe on a USB stick or in cloud storage. Restore
  asks for the passphrase, and a wrong one changes nothing. Plain zip backups still work both ways.
- **Server monitor**: the status bar shows the round trip to the active SSH tab's server, and its load,
  memory and disk use, read every ten seconds on a channel of its own, amber when high and red when
  critical. Settings, Behaviour turns it off; a server that cannot give the figures still has its round trip.
- **Server history**: a click on the monitor's figures, or Server Monitor in the command palette, shows
  the last half hour of the active tab's server, one small chart each for the round trip, load, memory
  and disk, with the time and value of any point under the pointer.
- **Server alerts**: when a connected server's disk, memory, load or round trip turns critical, a desktop
  notification (or a notice, while the window is in front) tells which; a click goes to its tab. Tabs not
  in front are checked once a minute. Each alert is told once, until the figure recovers; Settings turns
  them off.
- **Delete a folder with files in it**: in the file browser, deleting a folder on the server removes what is
  in it too (the question says so), instead of being refused because it was not empty. Links are removed,
  never followed.
- **Clear History**: in the import menu and the command palette, forgets the commands sent from the send bar,
  the addresses typed into quick connect, the recent sessions on the start page and the closed tabs that can be
  reopened. Saved sessions are kept.
- **Clear Logs**: in the import menu and the command palette, deletes the session log files in the log
  folder, except those an open tab is still writing to.
- **Transfer log**: the file browser's log is kept between sessions, shows completed transfers in green, and
  has a right-click menu to copy lines, copy all, save to a file or clear; a Clear button sits beside Log.
- **Modern file browser look**: softer rounded cards, flat toolbar buttons and field backgrounds, roomier rows,
  and no divider lines. The folder trees start hidden so the lists get the whole card; the folder button in each
  side's header shows them (the choice is remembered).
- **Compact file browser**: tighter cards, toolbar, rows and footer, and the transfer queue is out of the way
  until something is queued, failed or done (the Queue button in the footer shows it any time).
- **Properties and folder size**: Properties in either side's right-click menu shows a file's or folder's
  details, and adds up a folder's total size (files and folders counted, links not followed) while it is open.
- **Open Terminal Here**: in the remote menu, types `cd` to the selected folder (or the one showing) in the
  session's terminal and brings it forward. The name is quoted, and one with control characters is refused.
- **File browser keys**: Ctrl+F goes to the filter, Ctrl+L to the path, Escape empties the filter and then
  returns to the list, and Ctrl+Shift+C (or Copy Path in the menu) copies the selected paths, or the folder.
- **File browser sort order**: the column and direction each list was last sorted by are remembered.
- **Save what quick connect opened**: a Save button right after connecting, and Save as Session in the
  tab's menu, keep the address as a session; the tab carries on as that session. No password is kept.
- **One copy at a time**: starting the program again brings the open window to the front, and sessions
  named on the command line open in it. `--list`, `--tasks` and `--run-task` still work while it is open.
- **Grid of panes**: three or four panes at once, in two columns.
- **Broadcast to chosen tabs**: send typed input only to the tabs you mark.
- **Automatic responses**: answer prompts such as `--More--` or `[yes/no]` per session.
- **Notify when output contains**: be alerted when some text appears in a tab.
- **Connection Info** and a **Tunnels** window: see, stop and start tunnels without reconnecting.
- **SSH agent forwarding**, **Send Break** and **Send Text File**.
- **Reopen closed tabs**, and a warning when a hidden tab loses its connection.

File transfer

- A redesigned two-sided file browser with folder trees and a transfer queue.
- The file browser shows each side's path as a row of folders to click, which turns back into a field
  to type a path in, and colours the icons of archives, images, code and documents.
- Several transfers at once, each with its own progress, plus a detailed progress line.
- **Compare** in the file browser: the local and the remote folder are compared all the way down, by
  size and date, and what differs is listed (only here, only there, newer on either side). Upload and
  Download then copy just the ticked differences, through the transfer queue. Names and patterns to leave
  out (.git, node_modules and the like to begin with) are remembered, and once the copies are done it
  compares again and says whether the folders now match.
- Faster transfers: a file of 16 MB or more goes in four parts at once, each over a channel of its own,
  and downloads ask for far more at a time over a wider window; in tests against a server 50 ms away,
  uploads were about twice and downloads three times as fast. Uploads to OpenSSH servers go in larger
  pieces too. With a speed limit a transfer keeps to one channel, and one stopped part way can still be
  continued.
- Progress in the taskbar, and a flash when transfers finish in the background.
- A File Transfer page in Settings.

Window

- A tidier top bar: the app's own icon as its logo, quick connect in the middle of the window with a
  prompt icon and its shortcut shown as a key, and the tool buttons in one rounded tray beside New
  Session, all of them pills of one height. Its suggestions mark an address typed before with a
  clock and a saved session with a server, and the shell menu has icons too.
- The session search has a search icon and shows its shortcut as a key, like quick connect, and the
  start page shows its shortcuts as keys, as Keyboard Shortcuts does.
- A cleaner About page: the version and licence as chips under the name, the author on a line of
  their own and named once, the system details in one card with the buttons that act on them, and
  the licence, credits and release notes in one section.
  Below 900 pixels wide the name gives way, so quick connect keeps its room.
- On Windows 11 the title bar takes the colour of the top bar beneath it, and dialogs' title bars their
  background, in either theme, so a window's title bar and its top bar read as one.
- The sidebar collapses to a slim rail of the saved sessions instead of going away: a tile each, with
  its initials in its label colour and a dot while it is connected. A click goes to the session's tab, or
  opens it; the rail also has search and a new session button.
- Sessions in the sidebar have the same tiles as the rail, initials in their label colour with the
  connection dot on the corner, in place of the colour bar and ring.
- A star at the end of each session row makes it a favourite with one click, and back; it shows filled
  on favourites and as an outline on the row pointed at.
- **Pin Tab** in the tab's menu and the command palette: a pinned tab moves to the left of its pane, shows a
  diamond instead of a close cross, and is left alone by Close Other Tabs, Close Tabs to the Right and
  Close Disconnected Tabs; closing it by hand asks first.
  Pins come back with "Reopen tabs at start" and with Reopen Closed Tab.
  Pinned tabs stay together at the left: a tab dragged in among them, or one of them dragged out, goes back
  when the mouse is released. A session can be set to "Open its tab pinned" in its window.
  The same from the sidebar: Open Its Tab Pinned in a session's menu. A pinned tab moved to another pane
  joins the pinned ones there, and the command palette lists pinned tabs first, as Pinned.
  The sidebar marks a session that opens pinned with a small diamond, Close All Unpinned Tabs (in Other Tabs
  and the command palette) closes the rest in one go, and "Reopen tabs at start" puts each tab back in the pane
  it was in, and as wide as it was. The rail of session tiles marks them with a diamond too, and Pin All Tabs
  and Unpin All Tabs (Other Tabs, command palette) change every tab at once.
  "Reopen tabs at start" also brings back the tab that was in front in each pane. In Settings, Behaviour, pinned
  tabs can show only their icon (the name is in the tooltip), and the number of open tabs from which the tab
  list has its filter box can be chosen.
  "Reopen tabs at start" remembers the pane in use too, and a workspace keeps its tabs' pins, panes, sizes and
  front tabs as well as the sessions. Pin Tab has a shortcut (Ctrl+Shift+I), and so has the list of open
  tabs (Ctrl+Shift+J).
- **All open tabs list**: a chevron beside the + button lists every open tab of every pane, with its state
  icon, for when too many are open to find one in the tab bar. With more than eight open it has a box to
  filter them by name or address; Enter goes to the first one left.
- **Session search tells why**: the sidebar search also looks in a session's jump host and tunnels, and a row
  found only by its note, jump host or tunnels shows that line under its name.
- Calmer tabs: the close cross shows on the current tab and the one pointed at, without the others
  changing width, and the current tab has a fine edge that joins it to the terminal below.
- A sun and moon button in the top bar switches between the light and dark themes. With the theme
  set to follow the system, it and the command palette now switch from the theme on screen.
- The clock in the top bar shows a dot when tasks are scheduled, amber while one runs and red when
  one last failed, and its tooltip says which, and when the next runs. A click on the logo opens About.

Speed

- Heavy output is drawn about four times faster.
- Search in a long scrollback, and large remote folders, respond instantly.
- Connecting tries all of a server's addresses side by side, so one dead address no longer
  delays the login.
- Faster start-up.

Security and robustness

- File names from a server can no longer write outside the download folder, and fetched
  scripts are opened in Notepad instead of being run.
- Damaged `.ppk` keys, malformed imports and misbehaving Telnet servers are handled safely.
- Importing an OpenSSH config with `Match` sections works, and never runs their commands.

Look and feel

- Windows and dialogs fit every screen size, including small laptops and portrait monitors.
- Visible keyboard focus everywhere, and names for screen readers.
- Consistent shortcuts, menus, titles and status messages; a shorter, grouped tab menu.
- Several tabs of the same session are numbered, "web (2)", so they can be told apart; messages
  about a tab and the window title use the same names.
- Deleting a saved session can be undone from the status bar, saved password included; a
  duplicated session offers to be edited; a session that is already open offers to go to its tab.
- A command typed into the send bar for a tab that is not connected is kept, and the status bar
  says why and offers to reconnect, instead of the command disappearing unsent.
- Quitting names the sessions that are still open, and the quit and close-tab questions offer
  "Don't ask again".
- A keyboard shortcuts window (Ctrl+Shift+H) and a smarter command palette; Page Up and Page
  Down move through it a screenful at a time.
- The sidebar search matches each word on its own, in any order, as the command palette does:
  "prod web" finds web-01 in the Production folder.
- Ticked checkboxes show a tick, including disabled ones, which looked unticked before.
- A status bar message that arrived while the window was hidden is no longer drawn over the
  connection state.
- The file browser: a selected folder no longer has a block of accent colour beside it, its folder
  trees use the same arrows as the session list, and an empty transfer list says what would be in it.
- A lighter, cleaner file browser: the two sides and the transfer queue are cards, each side headed
  by what it is ("This computer" or the session's name), the path and filter fields carry icons in
  place of labels, columns have short names (Name, Size, Type, Modified), the queue's tabs sit on
  top, and Upload and Download are a quieter pair. It opens with the focus in the local files.
- With several panes, only the active one's tab is marked in the accent colour, so it is clear
  which pane typing and the send bar go to.
- The file browser's folder trees are taller and less indented, and keep the current folder in
  view while the folders around it are still being listed. Sizes are headed on the right, over the
  numbers, and the sort arrow sits above its heading, as in Explorer.
- A cleaner session dialog, start page and file browser.
- The product name is shown as Tech X Terminal everywhere.

Packaging

- A 64-bit Windows installer and a portable single-file exe, with checksums.
- Portable mode: a `TechXTerminal-data` folder next to the program keeps everything with it.
- The About page shows version details, the licence, release notes and third-party licences,
  all readable offline. The author is credited there; the top bar reads "Made with ♥ · © 2026".
- `--version` on the command line.

Fixes

- Transfers larger than 2 GB show their progress.
- Several remote tunnels on one session each reach their own destination.
- Text after a zero-width character in the output is no longer cut off.

## 0.3.0

Robustness

- An unexpected internal error is written to `~/.techxterminal/error.log` and reported in the
  status bar; the application keeps running instead of closing with every session.
- Closing a tab before its connection had started could still connect in the background and, if
  that connection asked for a password, close the application. A closed tab no longer connects.
- Saved sessions and saved commands are written in one step, so a crash or power cut during a
  save cannot leave half a file.
- A sessions file that cannot be read is kept as `sessions.json.corrupt` and reported, instead
  of being silently replaced by an empty list on the next save. Wrong values inside one session
  fall back to defaults and the rest loads.
- Settings that make no sense (for example after editing them by hand) fall back to defaults.
- Background work that is still busy when its window closes (a file transfer, a connection test,
  a key installation) is left to finish safely instead of being cut off.

File transfer

- Pause and resume. Interrupted transfers leave a `.part` file and continue from it next time.
- The speed limit now also takes effect at once on a download that started unlimited.
- Permissions column and editing, name filters, bookmarks; transferred files keep their dates.

Sessions and connections

- Protocols: SSH, Telnet, Rlogin, Raw and Serial; automatic logon with a wait period.
- SSH: two-factor logins, `.ppk` keys, several jump hosts, local and remote tunnels, a SOCKS proxy
  (optionally with a login), key pair generation and public key installation.
- Folders, favourites, notes, colours, recent sessions, drag between folders, import from an
  OpenSSH config and from Saved Sessions, export and import of the session list, full backup
  and restore, a connection test, and opening sessions from the command line.
- Per-session character set, Backspace and Enter behaviour, and local echo.

Terminal

- Mouse support for remote programs, find with regular expressions, links, keyword highlighting,
  colour schemes, visual bell, right-click paste, save output, logging with optional timestamps.

Window

- Tabs in up to four panes, side by side or stacked; unread marker; tab renaming; tab management.
- File browser window with two sides, drag and drop, speed limit, cancel, edit in place, a log.
- Quick switcher and command palette, saved commands, send bar history, dark and light themes,
  settings in pages, responsive layout, full screen, reopening tabs at start.

## 0.2.0

- Two-factor login, mouse reporting, `.ppk` keys, find, session import, logging, links, paste
  confirmation, automatic reconnect, split panes, file transfer, jump host and tunnels, a Windows
  build script and an end-to-end test.

## 0.1.0

- First version: tabbed SSH terminal with saved sessions.
