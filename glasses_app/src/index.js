/**
 * VR_RAG on Meta VR Glasses - entry point.
 * World.create() builds the IWSDK world from iwsdk.config.json (virtual:iwsdk-project),
 * then setupRag() adds the RAG answer panel + PDF page panel.
 */
import { World } from '@iwsdk/core';
import projectOptions from 'virtual:iwsdk-project';
import { setupRag } from './rag-scene.js';

World.create(document.getElementById('scene-container'), projectOptions).then((world) => {
  setupRag(world);
}).catch((e) => {
  console.error('VR_RAG failed to start:', e);
  document.body.insertAdjacentHTML('beforeend', '<pre style="position:fixed;top:8px;left:8px;color:#ff8a8a;background:#201015;padding:10px;z-index:99999">VR_RAG failed to start: ' + String(e && e.stack || e).replace(/</g, '&lt;') + '</pre>');
});
