import asyncio
import json
import websockets
import discordS
from discord.ext import voice_recv

GLADIA_API_KEY = "your_gladia_api_key_here"

class GladiaStreamingSink(voice_recv.AudioSink):
    def __init__(self, bot_loop):
        super().__init__()
        self.bot_loop = bot_loop
        self.user_sockets = {}  # Maps user_id -> active WebSocket connection

    def wants_local(self) -> bool:
        return False

    def receive(self, packet: voice_recv.VoicePacket):
        if not packet.user or packet.raw_data is None:
            return
        
        user_id = packet.user.id
        
        # If this user started talking and doesn't have a live stream yet, boot one up
        if user_id not in self.user_sockets:
            self.user_sockets[user_id] = "connecting"  # Placeholder to prevent double creation
            asyncio.run_coroutine_threadsafe(self.start_gladia_stream(user_id), self.bot_loop)
            
        # If the socket is active, forward the raw PCM bytes instantly
        ws = self.user_sockets.get(user_id)
        if ws and ws != "connecting" and ws.open:
            # Discord streams 48kHz stereo or mono usually; Gladia prefers 16kHz or 48kHz PCM.
            # Make sure your config matches what Discord sends (usually 48000Hz, 16bit, mono/stereo)
            asyncio.run_coroutine_threadsafe(ws.send(packet.pcm), self.bot_loop)

    async def start_gladia_stream(self, user_id):
        """Opens a unique live transcription pipeline for a specific user."""
        url = "wss://api.gladia.io/audio/text/audio-transcription-led/v2/live"
        
        # Gladia initial configuration frame
        config = {
            "x_gladia_key": GLADIA_API_KEY,
            "encoding": "pcm_s16le",       # 16-bit Little-Endian signed PCM
            "sample_rate": 48000,          # Discord native sample rate
            "channels": 1,                 # Adjust if capturing stereo
            "language": "en"               # Hardcode or use "auto"
        }

        try:
            async with websockets.connect(url) as ws:
                # 1. Initialize the session by sending the config JSON first
                await ws.send(json.dumps(config))
                self.user_sockets[user_id] = ws
                print(f"Connected to Gladia live stream for user {user_id}")

                # 2. Listen for incoming live text responses from Gladia
                async for message in ws:
                    response = json.loads(message)
                    
                    # Gladia fires 'transcript' events
                    if response.get("event") == "transcript":
                        transcription_data = response.get("transcription", {})
                        text = transcription_data.get("utterance", "")
                        
                        # Is the user still speaking or have they completed the sentence?
                        if transcription_data.get("is_final"):
                            text = transcription_data.get("utterance", "").strip()
                            if text:
                                print(f"[FINAL] User {user_id}: {text}")
                                # Hand off finalized text to your AI function safely
                                asyncio.create_task(self.ai_callback(user_id, text))
                        else:
                            print(f"[Interim] User {user_id}: {text}")
                            
        except Exception as e:
            print(f"Gladia stream error for user {user_id}: {e}")
        finally:
            if user_id in self.user_sockets:
                del self.user_sockets[user_id]

    def cleanup(self):
        # Close all active sockets when disconnecting from voice
        for ws in self.user_sockets.values():
            if ws != "connecting" and ws.open:
                asyncio.run_coroutine_threadsafe(ws.close(), self.bot_loop)
        self.user_sockets.clear()
