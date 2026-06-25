/**
 * Virtual Lobby - Main Entry Point
 *
 * A Tauri desktop app that hosts a virtual 3D sandbox where browser users
 * can spawn "players" (cubes) and control them like robots.
 */

import { Editor2D } from './editor/Editor2D';
import { Scene3D } from './renderer/Scene3D';
import { ApiClient } from './ApiClient';
import { WorldState } from './WorldState';

// State management
const worldState = new WorldState();
let apiClient: ApiClient | null = null;

// UI elements
const lobbyKeyInput = document.getElementById('lobby-key') as HTMLInputElement;
const connectBtn = document.getElementById('connect-btn') as HTMLButtonElement;
const connectionStatus = document.getElementById('connection-status') as HTMLDivElement;
const connectionLabel = document.getElementById('connection-label') as HTMLSpanElement;
const elementCount = document.getElementById('element-count') as HTMLSpanElement;
const playerCount = document.getElementById('player-count') as HTMLSpanElement;
const playersList = document.getElementById('players-list') as HTMLDivElement;
const editorCanvas = document.getElementById('editor-canvas') as HTMLCanvasElement;
const rendererCanvas = document.getElementById('renderer-canvas') as HTMLCanvasElement;
const toolButtons = document.querySelectorAll('[data-tool]') as NodeListOf<HTMLButtonElement>;
const zoomSlider = document.getElementById('zoom-slider') as HTMLInputElement;
const zoomLabel = document.getElementById('zoom-label') as HTMLSpanElement;
const resetViewBtn = document.getElementById('reset-view') as HTMLButtonElement;

// Initialize editor and renderer
const editor = new Editor2D(editorCanvas, worldState);
const scene = new Scene3D(rendererCanvas, worldState);

// Tool selection
let currentTool = 'select';
toolButtons.forEach(btn => {
  btn.addEventListener('click', () => {
    currentTool = btn.dataset.tool || 'select';
    toolButtons.forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    editor.setTool(currentTool);
  });
});

// Zoom controls
zoomSlider.addEventListener('input', () => {
  const zoom = parseFloat(zoomSlider.value);
  zoomLabel.textContent = `${Math.round(zoom * 100)}%`;
  editor.setZoom(zoom);
});

resetViewBtn.addEventListener('click', () => {
  zoomSlider.value = '1';
  zoomLabel.textContent = '100%';
  editor.resetView();
});

// Connection handling
connectBtn.addEventListener('click', async () => {
  const lobbyKey = lobbyKeyInput.value.trim();
  if (!lobbyKey) {
    alert('Please enter a lobby key');
    return;
  }

  if (apiClient?.isConnected()) {
    apiClient.disconnect();
    return;
  }

  try {
    connectBtn.textContent = 'Connecting...';
    connectBtn.disabled = true;

    apiClient = new ApiClient(lobbyKey, worldState);
    await apiClient.connect();

    updateConnectionUI(true);
  } catch (error) {
    console.error('Connection failed:', error);
    updateConnectionUI(false);
    alert(`Connection failed: ${error}`);
  } finally {
    connectBtn.disabled = false;
  }
});

function updateConnectionUI(connected: boolean) {
  connectionStatus.classList.toggle('connected', connected);
  connectionLabel.textContent = connected ? 'Connected' : 'Disconnected';
  connectBtn.textContent = connected ? 'Disconnect' : 'Connect';
}

// World state update handlers
worldState.onUpdate(() => {
  elementCount.textContent = String(worldState.elements.length);
  playerCount.textContent = String(worldState.players.length);
  renderPlayersList();
  editor.render();
  scene.render();
});

function renderPlayersList() {
  if (worldState.players.length === 0) {
    playersList.innerHTML = `
      <div class="player-item" style="color:#64748b;font-style:italic;">
        No players yet
      </div>
    `;
    return;
  }

  playersList.innerHTML = worldState.players.map(player => `
    <div class="player-item">
      <div class="player-color" style="background:${player.color}"></div>
      <span class="player-name">${player.name}</span>
    </div>
  `).join('');
}

// Animation loop
function animate() {
  scene.update();
  requestAnimationFrame(animate);
}

// Handle window resize
window.addEventListener('resize', () => {
  editor.resize();
  scene.resize();
});

// Initialize
editor.resize();
scene.resize();
animate();

console.log('Virtual Lobby initialized');
