// codes.js - read QR codes and barcodes (asset tags, part labels) from a picture, on this computer.
// Uses the browser's BarcodeDetector when it exists, otherwise ZXing (works in every browser).
import { applyFilter } from './media.js';

let zx = null;
async function zxing() {
  if (!zx) {
    const { BrowserMultiFormatReader } = await import('@zxing/browser');
    const { DecodeHintType, BarcodeFormat } = await import('@zxing/library');
    const hints = new Map();
    hints.set(DecodeHintType.TRY_HARDER, true);
    hints.set(DecodeHintType.POSSIBLE_FORMATS, [BarcodeFormat.QR_CODE, BarcodeFormat.DATA_MATRIX, BarcodeFormat.CODE_128,
      BarcodeFormat.CODE_39, BarcodeFormat.EAN_13, BarcodeFormat.EAN_8, BarcodeFormat.UPC_A, BarcodeFormat.ITF, BarcodeFormat.PDF_417, BarcodeFormat.AZTEC]);
    zx = new BrowserMultiFormatReader(hints);
  }
  return zx;
}

function copy(c) { const o = document.createElement('canvas'); o.width = c.width; o.height = c.height; o.getContext('2d').drawImage(c, 0, 0); return o; }

/** -> [{ text, format }] (empty when no code is found) */
export async function readCodes(canvas) {
  if ('BarcodeDetector' in window) {
    try {
      const det = new window.BarcodeDetector();
      const r = await det.detect(canvas);
      if (r.length) return r.map((x) => ({ text: x.rawValue, format: x.format }));
    } catch { /* fall back to ZXing */ }
  }
  const reader = await zxing();
  for (const variant of [canvas, applyFilter(copy(canvas), 'ocr')]) {      // second try: high-contrast copy
    try { const r = reader.decodeFromCanvas(variant); if (r) return [{ text: r.getText(), format: String(r.getBarcodeFormat()) }]; }
    catch { /* not found in this variant */ }
  }
  return [];
}
