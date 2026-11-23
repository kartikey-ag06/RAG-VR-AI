# 📖 User Manual: VR_RAG (Smart Glasses AI Assistant)

Welcome to **VR_RAG**! This user manual explains how to interact with the deployed smart glasses AI assistant directly in your web browser or inside a VR headset. No special hardware installation or technical setup is required.

---

## 🌐 1. Accessing the Application

1. Open your deployed application URL in any modern web browser (Google Chrome, Microsoft Edge, or Brave):
   ```text
   https://<your-deployed-domain>.vercel.app
   ```
2. When prompted by your browser, click **Allow** for **Microphone access** (needed for hands-free voice questions).
3. If using a physical VR headset (such as Meta Quest):
   - Open the **Meta Quest Browser** inside the headset.
   - Navigate to your deployed URL.

---

## 🥽 2. Entering the VR / AR Experience

Once the web page loads:

1. Click the **Enter XR** button on the screen.
2. You will be placed inside an augmented reality room (simulating what you see through Meta Smart Glasses).
3. Two interactive floating panels will appear in your field of view:
   - **Left / Center Panel:** Displays the AI-generated answer summary and source references.
   - **Right Panel:** Displays the exact, high-resolution original document page where the answer was cited.

---

## 🎮 3. How to Navigate and Control

### On Desktop / Laptop (Browser Simulator)
No VR equipment is needed to test and use the app:

| Action | How to do it |
|---|---|
| **Look around** | Click **Play Mode** (or click anywhere inside the 3D scene) to lock your cursor. Move your mouse to look in any direction. Press **Esc** at any time to release your mouse. |
| **Move around the room** | Hold **Left Shift** + use **W, A, S, D** to walk around. |
| **Switch Rooms** | Click the **Sofa icon** in the emulator toolbar to change the background environment (Kitchen, Office, Living Room, Bedroom). |
| **Next / Previous Page** | Press **Right Arrow** / **Left Arrow** on your keyboard (or use the on-screen buttons). |
| **Next Source Citation** | Press the **Tab** key to jump to the next cited document reference. |

### On a Physical VR Headset
- **Gaze / Head tracking:** Look naturally in any direction.
- **Pinch (Index finger + Thumb):** Advance to the next document page.
- **Squeeze / Grip:** Advance to the next cited source.
- **Controllers:** Use the trigger button to turn pages and the grip button to cycle sources.

---

## 🗣️ 4. Asking Questions

### Method A: Voice Query (Hands-Free)
1. **Hold the `V` key** on your keyboard (or click the **🎤 Speak** button in the VR panel).
2. Ask your question naturally (for example: *"What were the penalties imposed by SEBI?"*).
3. Release the key (or click the microphone icon again to stop).
4. The cloud AI transcribes your speech with Whisper, searches your documents, and updates both the answer panel and document page in real time.

### Method B: Text Query
- Click into the question input field on the control panel, type your question, and press **Enter**.
- You can also click any of the **Sample Question** buttons to test immediate responses.

---

## 📷 5. "Scan, Then Ask" (Camera & Visual Recognition)

You can inspect real-world or virtual objects and ask questions about them:

### A. In-VR Viewfinder Scan (Look & Scan)
1. In the 3D scene, look directly at an object so it sits inside the **red reticle ring** in the center of your view.
2. Press the **X key** on your keyboard (or click **🔍 Scan**).
3. The AI takes a snapshot through your simulated glasses, recognizes the object, checks your document library, and places the answer directly in front of you.

### B. Webcam or Photo Upload
1. Click the **📷 Photo** button on the control panel.
2. Choose **Capture Photo** (to take a photo with your computer's webcam) or **Upload a photo instead**.
3. Ask your question by voice or text. The AI analyzes the image and synthesizes an answer using your documents.
4. To dismiss an active photo, click the **✕** button on the photo preview thumbnail.

---

## 📄 6. Uploading and Managing Documents

You can add your own company manuals, legal documents, or procedure guides:

1. Click **＋ Upload docs** at the bottom of the interface (or visit the `/upload` path on your deployed domain).
2. Drag and drop any **PDF, Word (.docx), or Text (.txt)** file (up to 50 MB each).
3. The cloud backend automatically:
   - Extracts and indexes the text.
   - Generates high-resolution A4 page images for the VR viewer.
   - Calculates semantic embeddings for hybrid search.
4. Return to your VR app—new documents are indexed and immediately queryable without reloading.

---

## 📊 7. ProcOps Operations Dashboard

For managers and team leads monitoring standard operating procedures (SOPs):

1. Click the **ProcOps** link (or navigate to `/procops` on your domain).
2. **Friction Heatmap:** View which document pages and procedure steps trigger the highest number of questions from users.
3. **Content Gaps:** Discover questions that users asked where no matching document answers existed.
4. **AI-Assisted Fixes:** Click a high-friction page to have AI draft an updated, clearer procedure version and approve it for immediate publication to everyone's glasses.

---

## 🪟 8. Virtual Companion Apps (☰ Apps)

The system supports multitasking by streaming companion web apps into floating VR windows:

1. Click the **☰ Apps** menu button in the glasses interface.
2. Select any service: **Chrome, WhatsApp, Gmail, YouTube, ProcOps, or Upload**.
3. The service opens in a separate browser tab.
4. Click **Show in VR** and select that tab in your browser's share prompt.
5. The tab will float inside your VR view as a live companion window that you can reference while keeping your hands free.

---

## 👓 9. Assistant Features (like Meta glasses)
The panel has three tabs: **🎤 Ask**, **🛠 Tools** and **⚙ Settings**.
- **Wake word**: in ⚙ Settings tick **"Hey Glasses" wake word**, then say *"Hey Glasses, what is the penalty?"* any time.
- **Languages**: ⚙ Settings → **I speak** (your voice) and **Answers in** (the AI's answers). Tick **Read answers aloud** to hear them.
- **Answer style**: *Full*, *Glance card* (3 short points + the cut-out diagram) or *Spoken* (one sentence, read aloud).
- **Captions**: 🛠 → **💬 Captions** shows what people say on the lens; **🌐 Translate** also translates it.
- **Live AI**: 🛠 → **👁 Live AI** – the AI looks at your view every 20 s (change in Settings) and writes a short note.
- **Reminders / notes / calendar / weather**: say *"remind me at 5 pm to…"*, *"take a note…"*, *"what's on today"*, *"weather in Delhi"*. Due reminders pop up on the lens.
- **Camera**: *"take a photo"*, *"start recording"*, **🎭 Filters** (OCR boost, low-light, edges, B&W, vintage, cartoon, AR glasses/hat), **🖼 Gallery** (download, ask AI). Faces are blurred before photos go to the AI (Settings).

## 🏭 10. Field-Service Features
- **Machine QR / barcode**: 🛠 → **🏷 QR / code** (or *"scan the QR code"*). If the code matches a machine id tagged on the upload page, answers use that machine's documents first (chip 🏷 at the top; ✕ to clear). Tag documents on the **Upload** page (Machine IDs field or the **Tags** button).
- **Guided procedures**: *"guide me through starting pump P-101"*, then *"next"* / *"back"* / *"repeat"* (or N / B keys, or pinch). Each step shows its source page.
- **Diagrams**: 🛠 → **📐 Diagram** shows the cut-out table / drawing of the current page.
- **Pins**: *"pin this"* places the page or answer in 3D space around you; look at it for 2 s to get it explained (look-to-ask). Pins also show on the **Workspace** page.
- **Reports**: *"export report"* → inspection report, incident log or work order as PDF / Word / JSON, or send it to a connector (set up on **Admin**).
- **Remote expert**: *"call expert"* (suggested automatically when the AI is unsure). The expert opens the **Expert** page, sees your view, talks to you and sends tips that appear on your lens.
- **Admin page**: create users and groups (document permissions), add connectors (webhook, email, Jira, ServiceNow, folder sync), copy the MCP setup for other AI assistants.

## 🎤 11. Talk, Meet and Move
- **Teleprompter**: 🛠 → **🎤 Teleprompter**. Type a topic and press **✍ Write** (the AI writes it from your documents), or paste your own text, then **▶ Start**. In Chrome / Edge the line you are saying lights up as you speak; elsewhere it scrolls at your speed (Settings). Keys: Space pause, ↑ ↓ line, + / − speed, Esc close. Voice: *"teleprompter for the pump safety briefing"*.
- **Meeting notes**: 🛠 → **📝 Meeting notes** (or *"start meeting notes"*). Everything said is written down. Press it again (or *"stop meeting notes"*) for the AI summary, decisions and action items. Say *"add the action items to my reminders"*. All meetings are on **Workspace → Meetings** (PDF, Word, Markdown).
- **Hand gestures**: 🛠 → **✋ Gestures** (needs a webcam). Pinch = talk, swipe = next / back, open palm (hold) = pause, thumbs up (hold) = scan. A small camera bubble shows your hand.
- **Directions**: 🛠 → **🧭 Directions** or *"walk to India Gate"*, *"directions from Connaught Place to India Gate by car"*. The next turn shows on the lens and moves on as you walk; say *"next"* to skip, *"stop navigation"* to end. Allow location for the site.
- **Your calendar**: **Workspace → 📅 Today → Link a Google / Outlook calendar**, paste the calendar's private ICS link. *"What's on today"* then lists your real meetings too.
- **Recording light**: a red **REC / CAM / SCREEN** or amber **MIC** light shows on the lens (and on the panel) whenever anything is being captured.

## 💡 12. Tips & Troubleshooting
- **Expert call does not connect at work?** Office networks often block direct video calls. Ask your admin to set up the TURN relay (`deploy/turn/`) and set `TURN_URLS` + `TURN_SECRET`.
- **Firefox / Safari:** wake word and captions work through the AI voice service instead of the browser (about 1 second slower).

- **Microphone not responding?**  
  Ensure microphone permissions are enabled for your deployed domain in your browser settings (click the tune/lock icon in the address bar).
- **Cursor locked in mouse-look mode?**  
  Press the **Esc** key on your keyboard to release the mouse cursor back to normal.
- **Audio cut off too soon?**  
  Hold the **V** key for at least 1 second while speaking clearly.
- **Optimal performance:**  
  Use hardware-accelerated browsers (Chrome or Edge recommended) with WebGL enabled.
