# 📖 User Manual: VR_RAG (Smart Glasses AI Assistant)

Welcome to **VR_RAG**! This guide explains how to use the smart glasses AI assistant step-by-step. You don't need any special hardware or VR headset to try it—everything runs right inside your web browser.

---

## 🚀 1. How to Launch the App

### Option A: Online (Deployed Website)
Simply open the website URL provided (e.g. your Vercel link `https://your-app.vercel.app`) in Google Chrome, Edge, or Brave.

### Option B: On Your Computer (Locally)
1. Double-click the file named **`START_VR_RAG.bat`**.
2. A window will open, and your web browser will automatically open to `http://localhost:8765`.
3. Keep the black terminal window open while using the app.

---

## 🥽 2. Entering VR / AR Mode

Once the page loads, you will see a preview screen with the built-in **Meta VR Glasses simulator**:

1. Click the **Enter XR** button (at the bottom or center of the screen).
2. The view changes to a virtual room (like a kitchen or living room) through simulated smart glasses.
3. You will see two floating screens in front of you:
   - **Left Screen:** Shows the AI's direct answer with references.
   - **Right Screen:** Displays the real document page where the answer was found.

---

## 🎮 3. How to Move & Look Around (Controls)

You don't need a headset—your keyboard and mouse control everything:

| Action | Control |
|---|---|
| **Look around** | Click **Play Mode** (or click inside the view) to lock the mouse cursor. Move your mouse to look in any direction. Press **Esc** to release the mouse. |
| **Move / Walk** | Hold **Left Shift** + use **W, A, S, D** keys to move your head around the room. |
| **Change Virtual Room** | In the emulator toolbar at the top/side, click the **Sofa icon** to switch rooms (Kitchen, Office, Living Room, Bedroom). |
| **Next Document Page** | Press **Right Arrow** or **Pinch** with virtual hands. |
| **Previous Document Page** | Press **Left Arrow**. |
| **Next Source Citation** | Press **Tab** or **Squeeze** with virtual hands. |

---

## 🗣️ 4. Asking Questions

You can ask questions using your voice or by typing:

### A. Ask by Voice (Recommended)
1. **Hold the `V` key** on your keyboard (or tap the large **🎤 Speak** button).
2. Speak your question clearly into your computer microphone.
3. Release the key (or click the mic button again to stop).
4. The AI transcribes your speech, searches your documents, and updates the floating VR panels automatically.
> *Note: If your browser asks for microphone permission, click **Allow**.*

### B. Ask by Typing
- Type your question in the question box and press **Enter**.
- Or click any of the **Sample Question** buttons to test immediate answers.

---

## 📷 5. "Scan, Then Ask" (Visual Questions)

You can show an object to the AI and ask questions about it:

### In-VR Reticle Scan (Look & Scan)
1. In VR, look at any object in the virtual room so it sits inside the **red circular ring** in the center of your view.
2. Press the **X key** on your keyboard (or click **🔍 Scan**).
3. The AI takes a snapshot through your glasses, identifies the object, checks your documents, and presents the answer right in front of your eyes.

### Webcam or Photo Upload
1. Click **📷 Photo** on the control panel.
2. Choose **Capture Photo** (using your webcam) or **Upload a photo instead**.
3. Ask your question by voice or text. The AI combines what it sees in the photo with information from your documents.

---

## 📄 6. Uploading Your Own Documents

To search through your own files instead of the sample documents:

1. Click **＋ Upload docs** at the bottom of the screen (or visit `/upload` in your browser).
2. Drag and drop any **PDF, Word (.docx), or Text (.txt)** file (up to 50 MB).
3. The system automatically reads the text, creates page previews, and indexes the document.
4. Go back to the VR app—your new documents are immediately available for questions without needing a restart!

---

## 🪟 7. Multi-App Windows (☰ Apps)

The smart glasses app lets you mirror external websites and tools as floating windows in VR:

1. Click the **☰ Apps** button on the panel.
2. Pick any app: **Chrome, WhatsApp, Gmail, YouTube, ProcOps, or Upload**.
3. It opens in a new tab. Click **Show in VR** and select that tab in Chrome's screen share prompt.
4. The window will float inside your VR view so you can reference it while you work.

---

## ❓ 8. Quick Troubleshooting & Tips

- **The microphone isn't listening?**  
  Look at your browser's address bar (near the URL lock icon) and ensure microphone access is set to **Allow**.
- **Can't move the camera?**  
  Click once inside the virtual room to enable mouse look. Press **Esc** on your keyboard whenever you want your mouse pointer back.
- **Want to clear a photo question?**  
  Click the small **✕** icon on the photo thumbnail in the control panel.
- **Working with real VR headsets?**  
  Open the deployed website directly inside your headset's browser (e.g. Meta Quest Browser) and click **Enter XR** for full 6DoF immersion.
