/**
 * WebRTC Video Streaming
 *
 * Captures rendered frames and streams them via WebRTC.
 * Handles data channels for control input and telemetry output.
 */

export interface StreamConfig {
  width: number;
  height: number;
  fps: number;
}

export class VideoStream {
  private canvas: HTMLCanvasElement;
  private pc: RTCPeerConnection | null = null;
  private dataChannel: RTCDataChannel | null = null;
  private stream: MediaStream | null = null;

  private onControlInput: ((data: any) => void) | null = null;

  constructor(canvas: HTMLCanvasElement) {
    this.canvas = canvas;
  }

  setControlHandler(handler: (data: any) => void) {
    this.onControlInput = handler;
  }

  async createOffer(iceServers: RTCIceServer[] = []): Promise<RTCSessionDescriptionInit> {
    this.pc = new RTCPeerConnection({
      iceServers: iceServers.length ? iceServers : [
        { urls: 'stun:stun.l.google.com:19302' },
      ],
    });

    // Capture canvas stream
    this.stream = this.canvas.captureStream(30);

    // Add video track
    for (const track of this.stream.getVideoTracks()) {
      this.pc.addTrack(track, this.stream);
    }

    // Create data channel for control input
    this.dataChannel = this.pc.createDataChannel('control', {
      ordered: true,
    });

    this.dataChannel.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (this.onControlInput) {
          this.onControlInput(data);
        }
      } catch (e) {
        console.error('[VideoStream] Failed to parse control message:', e);
      }
    };

    // Create offer
    const offer = await this.pc.createOffer();
    await this.pc.setLocalDescription(offer);

    // Wait for ICE gathering
    await new Promise<void>((resolve) => {
      if (this.pc!.iceGatheringState === 'complete') {
        resolve();
      } else {
        this.pc!.onicegatheringstatechange = () => {
          if (this.pc!.iceGatheringState === 'complete') {
            resolve();
          }
        };
      }
    });

    return this.pc.localDescription!;
  }

  async setAnswer(answer: RTCSessionDescriptionInit): Promise<void> {
    if (!this.pc) throw new Error('No peer connection');
    await this.pc.setRemoteDescription(answer);
  }

  sendTelemetry(data: any) {
    if (this.dataChannel && this.dataChannel.readyState === 'open') {
      this.dataChannel.send(JSON.stringify(data));
    }
  }

  close() {
    if (this.stream) {
      for (const track of this.stream.getTracks()) {
        track.stop();
      }
      this.stream = null;
    }

    if (this.pc) {
      this.pc.close();
      this.pc = null;
    }

    this.dataChannel = null;
  }
}
