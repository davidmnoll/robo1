/**
 * World State Management
 *
 * Stores and manages the virtual world state including elements and players.
 * Notifies listeners when state changes.
 */

export interface VirtualElement {
  id: number;
  element_type: string;
  x: number;
  y: number;
  z: number;
  width?: number;
  height?: number;
  depth?: number;
  rotation: number;
  data?: string;
}

export interface VirtualPlayer {
  id: number;
  namespace: string;
  name: string;
  x: number;
  y: number;
  z: number;
  yaw: number;
  color: string;
}

type UpdateCallback = () => void;

export class WorldState {
  elements: VirtualElement[] = [];
  players: VirtualPlayer[] = [];
  private listeners: UpdateCallback[] = [];

  constructor() {}

  onUpdate(callback: UpdateCallback) {
    this.listeners.push(callback);
  }

  private notifyListeners() {
    this.listeners.forEach(cb => cb());
  }

  // Full state sync
  setWorldState(elements: VirtualElement[], players: VirtualPlayer[]) {
    this.elements = elements;
    this.players = players;
    this.notifyListeners();
  }

  // Element operations
  addElement(element: VirtualElement) {
    this.elements.push(element);
    this.notifyListeners();
  }

  updateElement(id: number, updates: Partial<VirtualElement>) {
    const element = this.elements.find(e => e.id === id);
    if (element) {
      Object.assign(element, updates);
      this.notifyListeners();
    }
  }

  removeElement(id: number) {
    this.elements = this.elements.filter(e => e.id !== id);
    this.notifyListeners();
  }

  getElementAt(x: number, y: number, threshold = 0.5): VirtualElement | undefined {
    return this.elements.find(e => {
      const dx = e.x - x;
      const dy = e.y - y;
      const halfW = (e.width || 1) / 2;
      const halfH = (e.height || 1) / 2;
      return Math.abs(dx) < halfW + threshold && Math.abs(dy) < halfH + threshold;
    });
  }

  // Player operations
  addPlayer(player: VirtualPlayer) {
    // Avoid duplicates
    if (!this.players.some(p => p.namespace === player.namespace)) {
      this.players.push(player);
      this.notifyListeners();
    }
  }

  updatePlayer(namespace: string, updates: Partial<VirtualPlayer>) {
    const player = this.players.find(p => p.namespace === namespace);
    if (player) {
      Object.assign(player, updates);
      this.notifyListeners();
    }
  }

  removePlayer(namespace: string) {
    this.players = this.players.filter(p => p.namespace !== namespace);
    this.notifyListeners();
  }

  getPlayer(namespace: string): VirtualPlayer | undefined {
    return this.players.find(p => p.namespace === namespace);
  }

  // Clear all state
  clear() {
    this.elements = [];
    this.players = [];
    this.notifyListeners();
  }
}
