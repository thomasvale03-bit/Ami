PLAYMAKER JARVIS - TAO DAILY VALIDATIONS (version 2)

WHAT IT DOES
- Every day at 10:00 AM, Jarvis opens TAO in its own private Microsoft Edge
  profile, filters Events I'm Promoting to "Past", finds the events that
  STARTED yesterday (an overnight event counts on the day it started),
  opens View Sales > Customers, and counts every redeemed ticket under
  "Redeemed Times". "Not Redeemed" tickets are never counted. A customer
  with 3 redeemed tickets counts as 3.
- The black-and-gold dashboard shows the total and the breakdown by event.
- Optional: a Spotify panel showing what is playing, with Play/Pause/Next.
- Jarvis only reads TAO. It never edits events, customers or sales.
- Your TAO password and verification code are typed into Edge by you and
  are never stored in these files.


BEFORE YOU START (one time)
1. Install Node.js "LTS" from https://nodejs.org (accept all defaults).
   Restart the computer afterwards.
2. Extract this ZIP to   C:\PlaymakerJarvis
   (Avoid Documents/Desktop if they sync to OneDrive.)


SETUP (one time)
1. Open C:\PlaymakerJarvis and double-click   Setup-Jarvis.bat
   It installs Jarvis, schedules the 10:00 AM check, and adds the
   dashboard to Windows startup.
2. An Edge window opens on the TAO login page. Sign in yourself.
3. When you can see "Events I'm Promoting", go back to the black setup
   window and press ENTER. You should see:
   "Secure TAO session saved on this computer."
4. Press ENTER to close the setup window.


FIRST TEST (please do this, and send me the result)
1. Double-click   Check-Now-Debug.bat
   An Edge window opens and you can watch Jarvis click through TAO.
   Do not click inside that window while it works.
2. When it finishes, the black window says either
   "Jarvis check finished successfully" or "did NOT finish successfully".
   The window stays open either way.
3. If it failed: double-click Open-Jarvis-Logs.bat and send me
   jarvis-error.log and the newest picture in the "screenshots" folder.
   (The screenshots can show customer names. Only share them with me.)
4. If it succeeded: compare the numbers with what TAO shows for one event.


EVERYDAY USE
- Start-Jarvis-Dashboard.bat   Opens the dashboard full-screen (F11 exits
                               full-screen). Opens automatically when you
                               sign in to Windows.
- Check-Now.bat                Runs a check right now (background browser).
                               The dashboard also has a CHECK NOW button.
- Check-Now-Debug.bat          Same check, but you can watch it in Edge.
- Start-Jarvis-Login.bat       Run this if the dashboard says TAO is not
                               signed in. Sign in again, then press ENTER.
- Open-Jarvis-Logs.bat         Opens the logs folder.

A small minimized window called "Playmaker Jarvis Server" runs the
dashboard. Leave it open. If you close it, run Start-Jarvis-Dashboard.bat.


SPOTIFY (optional, about 5 minutes, one time)
1. Go to https://developer.spotify.com/dashboard and log in with your
   Spotify account. Click "Create app".
2. Name: Playmaker Jarvis. Description: dashboard.
   Redirect URI: http://127.0.0.1:8787/spotify/callback   (exactly)
   Tick "Web API", accept the terms, and save.
3. Open the app's Settings and copy the "Client ID".
4. Open config.json in Notepad and paste it between the quotes:
     "spotifyClientId": "paste-it-here"
   Save the file.
5. Close the "Playmaker Jarvis Server" window, then run
   Start-Jarvis-Dashboard.bat again.
6. Click CONNECT SPOTIFY on the dashboard and approve. Jarvis stays signed
   in on this computer after that.
Notes: Jarvis shows and controls whatever Spotify is playing on (the
Spotify app on this PC, your phone, a speaker). It does not play audio by
itself. Play/Pause/Next need Spotify Premium.


IMPORTANT
- The computer must be on and signed in to Windows at 10:00 AM. If it was
  asleep or off, Windows runs the check as soon as it can.
- TAO sign-ins expire eventually. When that happens the dashboard shows a
  red message telling you to run Start-Jarvis-Login.bat.
- If TAO changes its website, the check may stop working. Run
  Check-Now-Debug.bat and send me logs\jarvis-error.log plus the newest
  screenshot.
- Settings are in config.json (check time, dashboard port). If you change
  scheduleTime, run Setup-Jarvis.bat again.


FILES THAT HOLD PRIVATE DATA (never share or upload these folders)
- .tao-session       Your signed-in TAO Edge profile.
- .spotify-session   Your Spotify sign-in.
- data, logs         Reports, logs and screenshots.
