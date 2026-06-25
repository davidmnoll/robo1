/**
 * API Client
 *
 * Handles WebSocket connection to the robot gateway API.
 * Syncs world state and relays player control/video.
 */

import { WorldState, VirtualElement, VirtualPlayer } from './WorldState';

const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8080';

export class ApiClient {
  private lobbyKey: string;
  private worldState: WorldState;
  private ws: WebSocket | null = null;
  private connected = false;
  private lobbyId: number | null = null;

  constructor(lobbyKey: string, worldState: WorldState) {
    this.lobbyKey = lobbyKey;
    this.worldState = worldState;
  }

  async connect(): Promise<void> {
    return new Promise((resolve, reject) => {
      const wsUrl = API_BASE.replace(/^http/, 'ws');
      const url = `${wsUrl}/api/internal/ws/lobbies?api_key=${encodeURIComponent(this.lobbyKey)}`;

      console.log('[ApiClient] Connecting to:', url);

      this.ws = new WebSocket(url);

      this.ws.onopen = () => {
        console.log('[ApiClient] Connected');
        this.connected = true;

        // Register as virtual lobby host (no physical robots)
        this.send({
          type: 'register_robots',
          robots: [],
        });

        // Request world state
        this.send({ type: 'get_world_state' });

        resolve();
      };

      this.ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          this.handleMessage(msg);
        } catch (e) {
          console.error('[ApiClient] Failed to parse message:', e);
        }
      };

      this.ws.onerror = (event) => {
        console.error('[ApiClient] WebSocket error:', event);
        if (!this.connected) {
          reject(new Error('WebSocket connection failed'));
        }
      };

      this.ws.onclose = () => {
        console.log('[ApiClient] Disconnected');
        this.connected = false;
        this.ws = null;
      };
    });
  }

  disconnect() {
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
    this.connected = false;
  }

  isConnected(): boolean {
    return this.connected;
  }

  private send(msg: Record<string, unknown>) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    }
  }

  private handleMessage(msg: Record<string, unknown>) {
    const msgType = msg.type as string;
    console.log('[ApiClient] Received:', msgType, msg);

    switch (msgType) {
      case 'registered':
        console.log('[ApiClient] Registered successfully');
        break;

      case 'world_state':
        this.lobbyId = msg.lobby_id as number;
        const elements = (msg.elements || []) as VirtualElement[];
        const players = (msg.players || []) as VirtualPlayer[];
        this.worldState.setWorldState(elements, players);
        break;

      case 'element_added':
        if (msg.element) {
          this.worldState.addElement(msg.element as VirtualElement);
        }
        break;

      case 'element_updated':
        if (msg.element) {
          const el = msg.element as VirtualElement;
          this.worldState.updateElement(el.id, el);
        }
        break;

      case 'element_removed':
        if (typeof msg.element_id === 'number') {
          this.worldState.removeElement(msg.element_id);
        }
        break;

      case 'virtual_player_created':
        if (msg.player) {
          this.worldState.addPlayer(msg.player as VirtualPlayer);
        }
        break;

      case 'virtual_player_deleted':
        if (msg.namespace) {
          this.worldState.removePlayer(msg.namespace as string);
        }
        break;

      case 'player_state':
        const namespace = msg.namespace as string;
        if (namespace) {
          this.worldState.updatePlayer(namespace, {
            x: msg.x as number | undefined,
            y: msg.y as number | undefined,
            z: msg.z as number | undefined,
            yaw: msg.yaw as number | undefined,
          });
        }
        break;

      case 'ack':
        // Acknowledgment received
        break;

      case 'error':
        console.error('[ApiClient] Error:', msg.error);
        break;

      default:
        console.log('[ApiClient] Unhandled message type:', msgType);
    }
  }

  // Element operations
  addElement(element: Omit<VirtualElement, 'id'>) {
    this.send({
      type: 'add_element',
      ...element,
    });
  }

  updateElement(id: number, updates: Partial<VirtualElement>) {
    this.send({
      type: 'update_element',
      element_id: id,
      ...updates,
    });
  }

  removeElement(id: number) {
    this.send({
      type: 'remove_element',
      element_id: id,
    });
  }

  // Virtual player operations
  registerVirtualPlayers(players: Array<{ namespace: string; name: string; color: string }>) {
    this.send({
      type: 'register_virtual_players',
      players,
    });
  }

  // Player state updates
  updatePlayerState(namespace: string, x: number, y: number, z: number, yaw: number) {
    this.send({
      type: 'player_state',
      namespace,
      x,
      y,
      z,
      yaw,
    });
  }
}
