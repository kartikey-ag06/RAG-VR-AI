// camera.js - "Scan, then ask": take a photo with the webcam (stands in for the glasses camera in the emulator)
// or upload one. The photo is shrunk to max 1024 px JPEG in the browser before it is sent.
// On a real Meta Quest the browser can expose the headset camera the same way (getUserMedia), with permission.

const MAX_SIDE = 1024;

/** Shrinks any image/video frame to a JPEG data URL. */
function toJpeg(source, w, h) {
  const s = Math.min(1, MAX_SIDE / Math.max(w, h));
  const c = document.createElement('canvas');
  c.width = Math.round(w * s); c.height = Math.round(h * s);
  c.getContext('2d').drawImage(source, 0, 0, c.width, c.height);
  return c.toDataURL('image/jpeg', 0.82);
}

export function fileToPhoto(file) {
  return new Promise((resolve, reject) => {
    if (!file || !/^image\//.test(file.type)) return reject(new Error('Please choose an image file'));
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => { URL.revokeObjectURL(url); resolve(toJpeg(img, img.naturalWidth, img.naturalHeight)); };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('Could not read that image')); };
    img.src = url;
  });
}

/**
 * Adds the scan dialog. opts: { button, onPhoto(dataUrl), onState(msg) }
 * Returns { open() }.
 */
export function setupCamera({ button, onPhoto, onState }) {
  const box = document.createElement('div');
  box.innerHTML = `
    <style>
      #cam-box{position:fixed;inset:0;z-index:2147483001;background:rgba(5,8,15,.82);display:none;align-items:center;justify-content:center}
      #cam-card{background:#0f1526;border:1px solid #2a3550;border-radius:12px;padding:14px;width:min(560px,94vw);color:#e8e8e8;font:13px Segoe UI,Arial,sans-serif}
      #cam-card h2{font-size:15px;margin:0 0 8px;color:#8cc8ff}
      #cam-video{width:100%;max-height:52vh;background:#000;border-radius:8px;display:block}
      #cam-msg{color:#ffb3c0;min-height:16px;margin:6px 0}
      #cam-card button,#cam-card label{margin:6px 6px 0 0;padding:7px 12px;border-radius:7px;border:1px solid #3a4766;background:#1b2540;color:#dfe8ff;cursor:pointer;font-size:12px;display:inline-block}
      #cam-snap{background:#2e7d4f !important;border-color:#3fb97a !important;font-weight:600}
      #cam-file{display:none}
    </style>
    <div id="cam-box"><div id="cam-card">
      <h2>📷 Scan an object, then ask about it</h2>
      <video id="cam-video" autoplay playsinline muted></video>
      <div id="cam-msg"></div>
      <button id="cam-snap">Capture photo</button>
      <label for="cam-file">Upload a photo instead</label><input id="cam-file" type="file" accept="image/*">
      <button id="cam-close">Cancel</button>
      <div style="font-size:11px;color:#7d879c;margin-top:8px">The photo is only sent to the AI for this question. It is not saved.</div>
    </div></div>`;
  document.body.appendChild(box);
  const $ = (id) => document.getElementById(id);
  const video = $('cam-video');
  let stream = null;

  // keep typed keys away from the emulator's movement keys while the dialog is open
  $('cam-box').addEventListener('keydown', (e) => e.stopPropagation(), true);

  function stop() {
    if (stream) stream.getTracks().forEach((t) => t.stop());
    stream = null; video.srcObject = null;
    $('cam-box').style.display = 'none';
  }

  async function open() {
    $('cam-box').style.display = 'flex'; $('cam-msg').textContent = '';
    $('cam-snap').disabled = true;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      $('cam-msg').textContent = 'No camera API in this browser - upload a photo instead.'; return;
    }
    try {
      stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment', width: { ideal: 1280 } }, audio: false });
      video.srcObject = stream;
      await video.play().catch(() => {});
      $('cam-snap').disabled = false;
    } catch (e) {
      $('cam-msg').textContent = (e.name === 'NotAllowedError' ? 'Camera permission was blocked.' : 'No camera found.') + ' You can upload a photo instead.';
    }
  }

  function done(photo) { stop(); onState && onState(''); onPhoto(photo); }

  $('cam-snap').onclick = () => {
    if (!video.videoWidth) { $('cam-msg').textContent = 'Camera is still starting...'; return; }
    done(toJpeg(video, video.videoWidth, video.videoHeight));
  };
  $('cam-file').onchange = async (e) => {
    try { done(await fileToPhoto(e.target.files[0])); } catch (err) { $('cam-msg').textContent = err.message; }
    e.target.value = '';
  };
  $('cam-close').onclick = stop;
  button.onclick = open;
  return { open, close: stop };
}
