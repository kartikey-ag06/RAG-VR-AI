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

## 💡 9. Tips & Troubleshooting

- **Microphone not responding?**  
  Ensure microphone permissions are enabled for your deployed domain in your browser settings (click the tune/lock icon in the address bar).
- **Cursor locked in mouse-look mode?**  
  Press the **Esc** key on your keyboard to release the mouse cursor back to normal.
- **Audio cut off too soon?**  
  Hold the **V** key for at least 1 second while speaking clearly.
- **Optimal performance:**  
  Use hardware-accelerated browsers (Chrome or Edge recommended) with WebGL enabled.
