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
});
