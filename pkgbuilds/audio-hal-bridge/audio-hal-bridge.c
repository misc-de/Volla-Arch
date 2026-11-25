// SPDX-License-Identifier: MIT
// audio-hal-bridge: the vendor's legacy audio HAL (audio.primary.*.so, loaded through
// libhybris) as PipeWire sinks and a source.
//
// The user sees only the phone's built-in devices, whatever the HAL routes behind them: two
// sinks, "Speaker" and "Earpiece", and one source, "Microphone", plus one sink "Headphones"
// while something is in the jack - one node per device, never one per codec, profile or rate.
// All sinks feed the HAL's one output stream, mixed; it is routed to the earpiece while only
// the earpiece sink plays, else to the speaker, so a sound never hides in the earpiece next to
// one on the speaker.
//
// The HAL paces both directions: a helper thread per direction blocks in the HAL's
// write()/read() and then drives the PipeWire graphs with pw_stream_trigger_process(), so the
// graphs run on the audio clock of the device. Data passes through a ring buffer per node,
// which also absorbs differences between the graph quantum and the HAL's transfer size. While
// no node of a direction is streaming (no client, or suspended by the session manager), its HAL
// stream goes to standby, which switches the amplifier or the microphone path off.
//
// Wired headphones or a headset (analog over USB-C) are a device of their own: a thread follows
// the switches of the jack's input device, reports the plug to the HAL as Android's audio policy
// does (connect=, without it the HAL opens no PCM for them) and the "Headphones" sink appears,
// which WirePlumber makes the default (plinius/new-device-default.lua), with the speaker's volume.
// While something is in the jack all output goes to it, the speaker sink's too: the HAL takes
// headphones plus speaker (routing=WIRED_HEADPHONE|SPEAKER) but switches on only the headphones
// (measured 2026-11-17), so a ringtone sounds in the headphones only. A headset's microphone
// still comes in behind "Microphone".
//
// Calls: the bridge serves the D-Bus API of callaudiod (org.mobian_project.CallAudio), which
// GNOME Calls uses. The modem's voice runs inside the device between modem and codec; the HAL
// only has to be in call mode and routed to earpiece, speaker or headset. When a call ends
// (SelectMode(0) after call mode; GNOME Calls asks for call mode only once a mobile call is
// active or dialling) a short jingle sounds where the call was: the bridge stays in call mode on
// the call's route (earpiece, speaker, or a Bluetooth headset over SCO, whose hands-free link
// stays meanwhile), mixes the jingle (/usr/share/audio-hal-bridge/call-end.wav) into its output
// as loud as the call, and only then goes to normal mode. Not with feedbackd's profile "silent";
// a call that starts meanwhile cuts it short. The reply to SelectMode does not wait for it.
//
// Conversations in apps (VoIP, SIP, a voice chat in the browser): while the microphone feeds a
// stream with the role "Communication" or "Phone", or a stream of a process that plays at the
// same time, the bridge cancels the echo of what it plays (WebRTC's AEC3, see aec.cpp), as a
// phone does; the reference is the exact mix it hands to the HAL. A browser's own canceller
// does not hold up against a speaker a few centimetres from the microphone (furios_audio), and
// the vendor's VoIP path through the legacy HAL cancels nothing (measured, docs/HARDWARE.md).
// Over the speaker the bridge also changes the acoustics: the speaker is two (the earpiece
// plays along on top), and the upper microphone hears little of the lower speaker. So during
// such a conversation the earpiece's amplifier is off and the upper microphone is used: the
// echo there is 16 dB below the voice instead of level with it (measured), which leaves the
// canceller room for both sides talking at once.
//
// Bluetooth headsets: music goes to the headset's own A2DP sink (PipeWire), without the HAL.
// A headset's microphone and its voice channel (SCO) are different: on this MediaTek phone SCO
// audio never crosses HCI (sco_rx stays 0 with a standing eSCO link, measured), the Bluetooth
// chip hands it to the application processor as ALSA device BTCVSD, and only the vendor HAL
// codes it. PipeWire's headset microphone (bluez_input.*) records digital silence. So the bridge
// offers the headset's microphone as a source of its own, named like the headset (the user sees
// one output and one input per device, never its profiles). While an app records from it, or a
// call runs (not on the speaker) while the headset is the default output, the bridge puts the
// headset into hands-free and carries the voice itself:
//   1. headset profile to hands-free (headset-head-unit), so an SCO link can exist;
//   2. a silent stream to the headset's hands-free sink holds the link (no stream, no link);
//   3. the HAL learns the codec (bt_wbs=on for mSBC) and BT_SCO=on, then both HAL streams are
//      reopened on the Bluetooth SCO devices: the HAL picks its PCM device while opening.
// Until the link stands (a second or two) the headset's source delivers silence, never the
// phone's microphone. Playback that wants the headset while it is in hands-free comes here
// instead (WirePlumber hook sco-to-hal.lua), and the headset sets the volume. Afterwards all of
// it is undone and the headset is back in A2DP. Measured and proven on the FuriPhone first
// (furios_audio), the same chip family.
//
// FM radio: the tuner (MediaTek connsys, /dev/fm) is switched on and tuned by the app that plays
// it (Emilia), the bridge only carries its sound. The chip hands it to the audio front end over
// I2S, and only the HAL routes it from there: an input stream on AUDIO_DEVICE_IN_FM_TUNER. The
// bridge offers it as a source of its own, "FM Radio", whose HAL stream is open only while
// something records from it. The app plays it like any other sound, so it reaches the speaker,
// wired or Bluetooth headphones alike. The cable of wired headphones is the antenna (without one
// the chip hears noise only, measured 2026-11-17). Below the microphone's priority: never the
// default source.
#include <android-config.h>
#include <hardware/audio.h>
#include <hardware/hardware.h>

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/input.h>
#include <math.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <time.h>
#include <unistd.h>

#include <alsa/asoundlib.h>
#include <gio/gio.h>
#include <glib-unix.h>
#include <pipewire/extensions/metadata.h>
#include <pipewire/pipewire.h>
#include <spa/param/audio/format-utils.h>
#include <spa/param/profile.h>
#include <spa/param/props.h>
#include <spa/pod/builder.h>
#include <spa/pod/parser.h>
#include <spa/utils/json.h>
#include <spa/utils/ringbuffer.h>

#include "aec.h"

#define RATE 48000
#define RING_SIZE (1u << 16) /* bytes, a power of two */
#define CHUNK_MAX 16384      /* largest HAL transfer we use, bytes */
#define AEC_BLOCK (RATE / 100) /* frames: the echo canceller works on 10 ms */
#define VOICE_MAX 64           /* voice streams and microphone links followed */
#define REF_BLOCKS 64          /* echo reference queue: 640 ms, far above the HAL's buffer */
#define BT_MAX 8               /* Bluetooth audio devices followed (nodes: twice as many) */
#define SCO_SWITCH_MS 8000     /* the headset has this long to take hands-free */
#define SCO_RELEASE_MS 1500    /* hands-free stays this long after the last use (apps reopen) */
/* Leaving standby the HAL takes its writes at once, but the amplifiers sound only about 0.45 s
 * later (measured 2026-11-15 with the microphone: the start of a beep train was missing, its end
 * on time); sounds shorter than that (dialog-information, audio-volume-change: 60 ms) were never
 * heard. So the first transfers after standby are silence, written before the graph is asked for
 * data: the sound comes that much later, but whole. */
#define OUT_WARMUP_MS 500
/* After the last sink stopped the output stays on this long, writing silence, so that short
 * sounds in a row (keys, a preview tapped again) do not each wait for the amplifiers */
#define OUT_LINGER_MS 3000
/* The jingle at the end of a call (see the top): 16 bit PCM, stereo, RATE; a longer file is not
 * taken. Call mode ends this long after the jingle's last transfer went to the HAL (it buffers
 * about 107 ms, measured), or at the latest CALL_END_SLACK_MS after the jingle's length. */
#define CALL_END_SOUND "/usr/share/audio-hal-bridge/call-end.wav"
#define CALL_END_MAX_S 3
#define CALL_END_TAIL_MS 200
#define CALL_END_SLACK_MS 2000

struct bridge;

/* A PipeWire node of the bridge */
struct port {
	struct bridge *bridge;
	bool playback;           /* PipeWire -> HAL (sink), else HAL -> PipeWire (source) */
	const char *name;        /* node.name */
	const char *description; /* the name the user sees */
	const char *icon;        /* device.icon-name */
	const char *priority;    /* priority.session: the speaker wins over the earpiece */
	uint32_t channels;
	uint32_t frame_size;
	size_t chunk;  /* bytes per HAL transfer: the HAL's period */
	size_t period; /* bytes per graph cycle as last requested (capture) */
	struct pw_stream *stream;
	struct spa_ringbuffer ring;
	uint8_t data[RING_SIZE];
	/* Lock and condition of the node's direction: the sinks share the output's */
	pthread_mutex_t *lock;
	pthread_cond_t *cond;
	bool active; /* stream is streaming */
	/* Graph cycles completed (trigger_done): the next cycle may only be triggered after the
	 * last one completed, else the apps on the graph miss cycles and their data is lost */
	unsigned long cycles;
	/* Sinks: 1 / the node's volume. PipeWire applies the volume before the data reaches us and
	 * the amplifier is loud, so the echo canceller gets the mix with the volume undone: its
	 * reference stays in proportion to the sound, at a level it takes for playing */
	_Atomic float unvolume;
	float volume, channel_volume; /* the node's, as PipeWire applies them (PipeWire thread) */
	_Atomic float gain;           /* volume * channel_volume, for the call volume */
	_Atomic float channel;        /* channel_volume, to give the sink back its volume */
};

struct bridge {
	struct pw_thread_loop *loop;
	struct audio_hw_device *dev;
	/* Output: two sinks on one HAL stream */
	struct port speaker;
	struct port earpiece;
	struct port phones; /* "Headphones": stream only while something is in the jack */
	atomic_bool phones_scheduled; /* headphones_update() is queued in the main loop */
	bool phones_fresh; /* just created: takes the speaker's volume (PipeWire thread) */
	struct audio_stream_out *out;
	pthread_t out_thread;
	pthread_mutex_t out_lock;
	pthread_cond_t out_cond;
	/* Input: both microphones as a stereo stream, channel 0 the lower, 1 the upper (measured) */
	struct port microphone;
	/* A connected Bluetooth headset's microphone, under the headset's name: a source of its own
	 * while the headset can do hands-free (bt_update), fed from the HAL on SCO */
	struct port bt_mic;
	char headset_name[128];
	uint32_t headset_device; /* bt_device id behind bt_mic */
	struct audio_stream_in *in;
	uint32_t in_channels; /* 2, or 1 if the HAL has no stereo input */
	pthread_t in_thread;
	pthread_mutex_t in_lock;
	pthread_cond_t in_cond;
	/* FM radio (see the top): a source of its own on an HAL input of its own, only with a tuner */
	struct port fm;
	bool has_fm;
	struct audio_stream_in *fm_in; /* fm thread only; NULL while nothing records */
	pthread_t fm_thread;
	pthread_mutex_t fm_lock;
	pthread_cond_t fm_cond;
	bool quit; /* set under all three port locks */
	int jack_fd; /* input device of the headset jack, -1 if none */
	pthread_t jack_thread;
	/* Routing state, under route_lock */
	pthread_mutex_t route_lock;
	bool headphones;    /* something in the jack */
	bool headset_mic;   /* ... with a microphone */
	audio_devices_t wired_out, wired_in; /* reported to the HAL as connected, 0: none */
	unsigned playing;   /* outside calls: the sinks that stream (SINK_*), as last routed */
	bool call;          /* voice call mode */
	bool call_speaker;  /* in a call: speaker instead of earpiece */
	bool mic_muted;
	/* The call volume (see apply_call_volume), main thread only */
	int call_step;                /* 1..CALL_STEPS, saved in the user's state directory */
	bool call_on;                 /* the call volume is in force */
	struct port *call_port;       /* the sink showing the call volume, NULL: none (headset) */
	float media_channel;          /* its channel volume before the call */
	/* The sink the volume keys change: the earpiece or the speaker if it is the default sink,
	 * else NULL (a headset sets its own volume). PipeWire thread writes, from the "default"
	 * metadata. */
	struct port *_Atomic volume_port;
	atomic_bool voice_scheduled;  /* call_volume_update() is queued in the main loop */
	/* With role based volumes (device-volla-ansuz 55-plinius-media-roles.conf) the call channel is
	 * the role sink input.loopback.sink.role.phone: its gain, < 0 while there is none. PipeWire
	 * thread writes. */
	_Atomic float role_phone_gain;
	float voice_applied;          /* the voice volume last handed to the HAL that way, main thread */
	audio_devices_t routed_out;   /* as last routed */
	atomic_bool speaker_routed;   /* routed_out is the speaker */
	atomic_bool speakerphone;     /* a conversation in an app over the speaker: see top */
	/* Echo cancellation, on while a voice stream records from the microphone */
	struct aec *aec; /* NULL: not available */
	atomic_bool aec_on;
	int aec_delay;                         /* ms from a HAL write to its sound in the mic */
	int16_t render_block[AEC_BLOCK * 2];   /* playback thread only */
	size_t render_fill;                    /* frames */
	/* Reference blocks on their way to the canceller: the playback thread queues what it hands
	 * to the HAL, the capture thread takes one block per microphone block. The canceller needs
	 * both sides at the same pace; fed from the playback thread they drifted apart by a second
	 * in half a minute (measured), at every start and stop of playback. The queue holds about
	 * what the HAL buffers (107 ms, measured), the delay the canceller should see. */
	pthread_mutex_t ref_lock;
	int16_t ref_queue[REF_BLOCKS][AEC_BLOCK * 2];
	size_t ref_head, ref_count;
	int16_t capture_block[AEC_BLOCK];      /* capture thread only */
	size_t capture_fill;                   /* frames */
	/* Registry watch for voice streams, in the PipeWire thread */
	struct pw_context *context;
	struct pw_core *core;
	struct pw_registry *registry;
	struct spa_hook registry_listener;
	/* App streams, bound for their full properties: the registry has the client only, and all
	 * apps of pipewire-pulse share its client process */
	struct stream_watch {
		struct bridge *bridge;
		bool used;
		uint32_t id, client;
		bool playback;  /* else capture */
		bool voice;     /* role of a call */
		char pid[16];   /* application.process.id, "" until known */
		struct pw_proxy *proxy;
		struct spa_hook listener;
	} streams[VOICE_MAX];
	struct {
		uint32_t link, node;
	} mic_links[VOICE_MAX]; /* links from the microphone and the node they feed */
	size_t n_mic_links;
	struct {
		uint32_t link, node;
	} headset_links[VOICE_MAX]; /* links from the headset's source and the node they feed */
	size_t n_headset_links;
	/* Bluetooth audio devices and their nodes, and the default sink: written in the PipeWire
	 * thread (registry), read in the main thread with the loop locked */
	struct bt_device {
		struct bridge *bridge;
		bool used;
		uint32_t id;
		int hfp, hfp_cvsd, a2dp; /* profile indices, -1: not offered */
		int current;             /* active profile index, -1: not known yet */
		struct pw_proxy *proxy;
		struct spa_hook listener;
	} bt_devices[BT_MAX];
	struct bt_node {
		struct bridge *bridge;
		bool used;
		bool sink;
		uint32_t id, device; /* device: id of its bt_device, 0 until known */
		char name[128], description[128], profile[48], codec[16];
		struct pw_proxy *proxy;
		struct spa_hook listener;
	} bt_nodes[BT_MAX * 2];
	char default_sink[128]; /* node name, default.audio.sink of the "default" metadata */
	struct pw_proxy *metadata;
	struct spa_hook metadata_listener;
	struct pw_proxy *role_phone;  /* the call channel's role sink, NULL: none */
	uint32_t role_phone_id;
	struct spa_hook role_phone_listener;
	atomic_bool bt_scheduled; /* bt_update() is queued in the main loop */
	/* Hands-free through the HAL (main thread, see the top) */
	enum { SCO_OFF, SCO_SWITCHING, SCO_ON } sco_state;
	uint32_t sco_device;   /* the headset in hands-free (bt_device id) */
	int sco_restore;       /* profile to go back to, -1: none */
	bool sco_blocked;      /* the headset refused: no new attempt until nothing needs it */
	char sco_codec[16];    /* codec the HAL was told */
	guint sco_timeout, sco_release;
	struct pw_stream *sco_hold; /* holds the SCO link */
	struct spa_hook sco_hold_listener;
	bool sco_hold_streaming;    /* PipeWire thread */
	atomic_bool sco;            /* HAL streams on Bluetooth SCO: route(), playback volume */
	/* Around the HAL calls on b->out and b->in, which hal_sco() replaces. Lock order:
	 * route_lock, then the port locks, then these. */
	pthread_mutex_t hal_out_lock, hal_in_lock;
	/* CallAudio D-Bus service, main loop */
	GMainLoop *main_loop;
	GDBusConnection *bus;
	/* The jingle at the end of a call (load_jingle), NULL: none */
	int16_t *jingle;
	size_t jingle_frames;
	/* Under out_lock: the playback thread mixes it while jingle_playing */
	bool jingle_playing;
	size_t jingle_pos; /* frames */
	float jingle_gain;
	unsigned jingle_gen;
	/* Under route_lock: the call ended, call mode stays until the jingle is through
	 * (call_end_start); call_end_gen tells a later call's end from this one's */
	bool call_ending;
	unsigned call_end_gen;
	guint call_end_timeout;
};

static void route(struct bridge *b);

/* The playback thread hands the end of the jingle to the main loop */
struct jingle_played {
	struct bridge *bridge;
	unsigned gen;
};
static gboolean on_jingle_played(gpointer data);

/* The sinks that stream, for route() */
#define SINK_SPEAKER 1u
#define SINK_EARPIECE 2u
#define SINK_PHONES 4u

static uint32_t ring_fill(struct port *p)
{
	uint32_t index;
	int32_t fill = spa_ringbuffer_get_read_index(&p->ring, &index);
	return fill > 0 ? (uint32_t)fill : 0;
}

static void ring_write(struct port *p, const void *src, uint32_t size)
{
	uint32_t index;
	int32_t fill = spa_ringbuffer_get_write_index(&p->ring, &index);
	if (fill < 0 || size > RING_SIZE - (uint32_t)fill)
		return; /* overrun: the consumer stalled, drop this block */
	spa_ringbuffer_write_data(&p->ring, p->data, RING_SIZE, index & (RING_SIZE - 1), src, size);
	spa_ringbuffer_write_update(&p->ring, index + size);
}

static uint32_t ring_read(struct port *p, void *dst, uint32_t size)
{
	uint32_t index;
	int32_t fill = spa_ringbuffer_get_read_index(&p->ring, &index);
	if (fill <= 0)
		return 0;
	if (size > (uint32_t)fill)
		size = fill;
	spa_ringbuffer_read_data(&p->ring, p->data, RING_SIZE, index & (RING_SIZE - 1), dst, size);
	spa_ringbuffer_read_update(&p->ring, index + size);
	return size;
}

/* The time 50 ms from now, for pthread_cond_timedwait() */
static void deadline(struct timespec *until)
{
	clock_gettime(CLOCK_REALTIME, until);
	until->tv_nsec += 50 * 1000000;
	if (until->tv_nsec >= 1000000000) {
		until->tv_sec++;
		until->tv_nsec -= 1000000000;
	}
}

/* Runs one graph cycle and waits until it completed. Call with p->lock held. Returns false if
 * the cycle did not complete in time (the graph may not have run: try again later). */
static bool run_cycle(struct port *p)
{
	unsigned long cycles = p->cycles;
	pthread_mutex_unlock(p->lock);
	pw_thread_loop_lock(p->bridge->loop);
	if (!p->stream) { /* the headset's source went away (bt_update) */
		pw_thread_loop_unlock(p->bridge->loop);
		pthread_mutex_lock(p->lock);
		return false;
	}
	bool driving = pw_stream_is_driving(p->stream);
	if (driving)
		pw_stream_trigger_process(p->stream);
	pw_thread_loop_unlock(p->bridge->loop);
	pthread_mutex_lock(p->lock);
	struct timespec until;
	deadline(&until);
	if (!driving) /* another driver runs the graph: wait for its next cycle (process()) */
		return pthread_cond_timedwait(p->cond, p->lock, &until) != ETIMEDOUT;
	while (p->cycles == cycles && p->active && !p->bridge->quit)
		if (pthread_cond_timedwait(p->cond, p->lock, &until) == ETIMEDOUT)
			break;
	return p->cycles != cycles;
}

/* The echo canceller's reference: what goes to the HAL now (stereo), queued in 10 ms blocks */
static void echo_reference(struct bridge *b, const int16_t *data, size_t frames)
{
	while (frames) {
		size_t n = SPA_MIN(frames, AEC_BLOCK - b->render_fill);
		memcpy(b->render_block + b->render_fill * 2, data, n * 4);
		b->render_fill += n;
		data += n * 2;
		frames -= n;
		if (b->render_fill == AEC_BLOCK) {
			pthread_mutex_lock(&b->ref_lock);
			if (b->ref_count == REF_BLOCKS) { /* the capture side stalled: drop the oldest */
				b->ref_head = (b->ref_head + 1) % REF_BLOCKS;
				b->ref_count--;
			}
			memcpy(b->ref_queue[(b->ref_head + b->ref_count) % REF_BLOCKS], b->render_block,
			       sizeof(b->render_block));
			b->ref_count++;
			pthread_mutex_unlock(&b->ref_lock);
			b->render_fill = 0;
		}
	}
}

/* Empties the reference queue: what is left belongs to another conversation */
static void echo_reference_reset(struct bridge *b)
{
	pthread_mutex_lock(&b->ref_lock);
	b->ref_head = b->ref_count = 0;
	pthread_mutex_unlock(&b->ref_lock);
	b->render_fill = 0;
}

/* Cancels the echo in the microphone signal (mono) in 10 ms blocks. Returns the bytes of the
 * whole blocks done in out; the rest waits for the next read. */
static size_t echo_cancel(struct bridge *b, const int16_t *in, size_t frames, int16_t *out)
{
	size_t done = 0;
	while (frames) {
		size_t n = SPA_MIN(frames, AEC_BLOCK - b->capture_fill);
		memcpy(b->capture_block + b->capture_fill, in, n * 2);
		b->capture_fill += n;
		in += n;
		frames -= n;
		if (b->capture_fill == AEC_BLOCK) {
			/* One reference block per microphone block: silence while nothing plays */
			static int16_t ref[AEC_BLOCK * 2];
			pthread_mutex_lock(&b->ref_lock);
			if (b->ref_count) {
				memcpy(ref, b->ref_queue[b->ref_head], sizeof(ref));
				b->ref_head = (b->ref_head + 1) % REF_BLOCKS;
				b->ref_count--;
			} else {
				memset(ref, 0, sizeof(ref));
			}
			pthread_mutex_unlock(&b->ref_lock);
			aec_render(b->aec, ref);
			aec_capture(b->aec, b->capture_block, b->aec_delay);
			memcpy(out + done, b->capture_block, sizeof(b->capture_block));
			done += AEC_BLOCK;
			b->capture_fill = 0;
		}
	}
	return done * 2;
}

/* The earpiece's amplifier (aw87xxx #1): in the speaker path the HAL sets it to "Music", as the
 * upper of two speakers. Writes only if the value differs. Returns 0 if it is as asked. */
static int set_earpiece_amp(bool on)
{
	snd_ctl_t *ctl;
	if (snd_ctl_open(&ctl, "hw:0", 0) < 0)
		return -1;
	snd_ctl_elem_id_t *id;
	snd_ctl_elem_info_t *info;
	snd_ctl_elem_value_t *value;
	snd_ctl_elem_id_alloca(&id);
	snd_ctl_elem_info_alloca(&info);
	snd_ctl_elem_value_alloca(&value);
	snd_ctl_elem_id_set_interface(id, SND_CTL_ELEM_IFACE_MIXER);
	snd_ctl_elem_id_set_name(id, "aw87xxx_profile_switch_1");
	snd_ctl_elem_info_set_id(info, id);
	int r = snd_ctl_elem_info(ctl, info);
	unsigned items = r < 0 ? 0 : snd_ctl_elem_info_get_items(info), item = items;
	for (unsigned i = 0; i < items && item == items; i++) {
		snd_ctl_elem_info_set_item(info, i);
		if (snd_ctl_elem_info(ctl, info) >= 0 && !strcmp(snd_ctl_elem_info_get_item_name(info), on ? "Music" : "Off"))
			item = i;
	}
	if (r >= 0 && item == items)
		r = -ENOENT;
	if (r >= 0) {
		snd_ctl_elem_value_set_id(value, id);
		r = snd_ctl_elem_read(ctl, value);
		if (r >= 0 && snd_ctl_elem_value_get_enumerated(value, 0) != item) {
			snd_ctl_elem_value_set_enumerated(value, 0, item);
			r = snd_ctl_elem_write(ctl, value);
		}
	}
	snd_ctl_close(ctl);
	return r < 0 ? r : 0;
}

/* A streaming sink with less than one transfer queued. Call with out_lock held. */
static bool sink_short(struct port *p)
{
	return p->active && ring_fill(p) < p->chunk;
}

/* Outputs: ask the graph of each streaming sink for data whenever less than one transfer is
 * queued, mix what the sinks queued and hand it to the HAL as one transfer. write() blocks
 * until the device has room, which paces the graphs. */
static void *playback_thread(void *data)
{
	struct bridge *b = data;
	struct port *sinks[] = {&b->speaker, &b->earpiece, &b->phones};
	size_t chunk = b->speaker.chunk;
	int16_t mix[CHUNK_MAX / 2], buf[CHUNK_MAX / 2], ref[CHUNK_MAX / 2];
	float unmixed[CHUNK_MAX / 2]; /* the mix with the volumes undone: echo reference */
	unsigned playing = 0; /* as last routed */
	bool earpiece_amp_off = false; /* turned off for a speakerphone conversation */
	bool referencing = false;      /* queued echo reference for the canceller */
	unsigned writes = 0;
	/* Transfers of silence: after standby (warm-up) and after the last sink stopped (linger) */
	size_t chunk_ms = SPA_MAX(chunk / 4 * 1000 / RATE, (size_t)1); /* stereo, 16 bit */
	unsigned warmup_chunks = (OUT_WARMUP_MS + chunk_ms - 1) / chunk_ms;
	unsigned linger_chunks = OUT_LINGER_MS / chunk_ms;
	bool standby = true;
	unsigned lingered = 0;

	memset(mix, 0, sizeof mix);
	pthread_mutex_lock(&b->out_lock);
	while (!b->quit) {
		bool any = b->speaker.active || b->earpiece.active || b->phones.active || b->jingle_playing;
		if (!any && !standby && lingered < linger_chunks) {
			/* What a stopped sink left queued is not played after a pause */
			if (lingered++ == 0)
				for (size_t i = 0; i < SPA_N_ELEMENTS(sinks); i++)
					spa_ringbuffer_init(&sinks[i]->ring);
			pthread_mutex_unlock(&b->out_lock);
			memset(mix, 0, chunk);
			pthread_mutex_lock(&b->hal_out_lock);
			b->out->write(b->out, mix, chunk);
			pthread_mutex_unlock(&b->hal_out_lock);
			pthread_mutex_lock(&b->out_lock);
			continue;
		}
		if (!any) {
			standby = true;
			earpiece_amp_off = false; /* standby turns the amplifiers off anyway */
			pthread_mutex_lock(&b->hal_out_lock);
			b->out->common.standby(&b->out->common);
			pthread_mutex_unlock(&b->hal_out_lock);
			for (size_t i = 0; i < SPA_N_ELEMENTS(sinks); i++)
				spa_ringbuffer_init(&sinks[i]->ring);
			pthread_cond_wait(&b->out_cond, &b->out_lock);
			continue;
		}
		lingered = 0;
		unsigned now = (b->speaker.active ? SINK_SPEAKER : 0) | (b->earpiece.active ? SINK_EARPIECE : 0) |
			       (b->phones.active ? SINK_PHONES : 0);
		if (playing != now) {
			playing = now;
			pthread_mutex_unlock(&b->out_lock);
			pthread_mutex_lock(&b->route_lock);
			b->playing = playing;
			route(b);
			pthread_mutex_unlock(&b->route_lock);
			pthread_mutex_lock(&b->out_lock);
			continue; /* the sinks may have changed meanwhile */
		}
		/* Out of standby: silence first, the graphs wait meanwhile (route() above has set the
		 * path the warm-up plays on) */
		if (standby) {
			standby = false;
			pthread_mutex_unlock(&b->out_lock);
			memset(mix, 0, chunk);
			for (unsigned i = 0; i < warmup_chunks && !b->quit; i++) {
				pthread_mutex_lock(&b->hal_out_lock);
				b->out->write(b->out, mix, chunk);
				pthread_mutex_unlock(&b->hal_out_lock);
			}
			pthread_mutex_lock(&b->out_lock);
			continue; /* the sinks may have changed meanwhile */
		}
		/* Graph cycles until each streaming sink has a transfer queued (the quantum may be
		 * smaller than the transfer) */
		for (size_t i = 0; i < SPA_N_ELEMENTS(sinks); i++)
			while (sink_short(sinks[i]) && !b->quit)
				if (!run_cycle(sinks[i]))
					break; /* underrun: what is missing stays silence */
		/* Underruns stay silence */
		bool aec_on = atomic_load(&b->aec_on);
		/* On a Bluetooth headset the headset sets the volume (hands-free volume, AT+VGS):
		 * the speaker's volume, which PipeWire applied, is undone */
		bool sco = atomic_load(&b->sco);
		memset(mix, 0, chunk);
		if (aec_on)
			memset(unmixed, 0, chunk * 2);
		for (size_t i = 0; i < SPA_N_ELEMENTS(sinks); i++) {
			if (!sinks[i]->active)
				continue;
			uint32_t got = ring_read(sinks[i], buf, chunk);
			float unvolume = atomic_load(&sinks[i]->unvolume);
			if (sco)
				for (uint32_t s = 0; s < got / 2; s++)
					mix[s] = SPA_CLAMP(mix[s] + (int32_t)(buf[s] * unvolume), INT16_MIN, INT16_MAX);
			else
				for (uint32_t s = 0; s < got / 2; s++)
					mix[s] = SPA_CLAMP(mix[s] + buf[s], INT16_MIN, INT16_MAX);
			for (uint32_t s = 0; aec_on && s < got / 2; s++)
				unmixed[s] += buf[s] * unvolume;
		}
		/* The jingle at the end of a call (call_end_start), on top of what the sinks play */
		struct jingle_played *played = NULL;
		if (b->jingle_playing) {
			size_t n = SPA_MIN(chunk / 4, b->jingle_frames - b->jingle_pos);
			const int16_t *j = b->jingle + b->jingle_pos * 2;
			for (size_t s = 0; s < n * 2; s++)
				mix[s] = SPA_CLAMP(mix[s] + (int32_t)(j[s] * b->jingle_gain), INT16_MIN, INT16_MAX);
			b->jingle_pos += n;
			if (b->jingle_pos == b->jingle_frames) {
				b->jingle_playing = false;
				played = g_new(struct jingle_played, 1);
				*played = (struct jingle_played){b, b->jingle_gen};
			}
		}
		pthread_mutex_unlock(&b->out_lock);
		if (aec_on) {
			for (size_t s = 0; s < chunk / 2; s++)
				ref[s] = SPA_CLAMP(unmixed[s], INT16_MIN, INT16_MAX);
			echo_reference(b, ref, chunk / 4);
			referencing = true;
		} else if (referencing) {
			echo_reference_reset(b);
			referencing = false;
		}
		pthread_mutex_lock(&b->hal_out_lock);
		b->out->write(b->out, mix, chunk);
		pthread_mutex_unlock(&b->hal_out_lock);
		if (played) /* its last transfer is with the HAL */
			g_idle_add(on_jingle_played, played);
		/* After the write: the HAL sets its path (both amplifiers on) when it leaves standby
		 * or changes the routing, so a speakerphone conversation checks about twice a second */
		writes++;
		if (atomic_load(&b->speakerphone)) {
			if ((!earpiece_amp_off || writes % 24 == 0) && !set_earpiece_amp(false))
				earpiece_amp_off = true;
		} else if (earpiece_amp_off) {
			/* Back to the HAL's speaker path; on another route the HAL has set it itself */
			if (atomic_load(&b->speaker_routed))
				set_earpiece_amp(true);
			earpiece_amp_off = false;
		}
		pthread_mutex_lock(&b->out_lock);
	}
	pthread_mutex_unlock(&b->out_lock);
	return NULL;
}

/* Sources (the microphone, a Bluetooth headset's microphone): read() whole HAL periods (smaller
 * reads still wait a full period), then run as many graph cycles per streaming source as there
 * are graph periods queued, each after the last one took its data. Both share one HAL input. */
static void *capture_thread(void *data)
{
	struct bridge *b = data;
	struct port *sources[] = {&b->microphone, &b->bt_mic};
	size_t chunk = b->microphone.chunk; /* the headset's source has the same */
	int16_t hal[CHUNK_MAX], buf[CHUNK_MAX / 2], cancelled[CHUNK_MAX / 2 + AEC_BLOCK];
	static const int16_t quiet[CHUNK_MAX / 2];

	pthread_mutex_lock(&b->in_lock);
	while (!b->quit) {
		bool mic = b->microphone.active, headset = b->bt_mic.active;
		if (!mic && !headset) {
			pthread_mutex_lock(&b->hal_in_lock);
			b->in->common.standby(&b->in->common);
			pthread_mutex_unlock(&b->hal_in_lock);
			for (size_t i = 0; i < SPA_N_ELEMENTS(sources); i++)
				spa_ringbuffer_init(&sources[i]->ring);
			pthread_cond_wait(&b->in_cond, &b->in_lock);
			continue;
		}
		pthread_mutex_unlock(&b->in_lock);
		/* The stream and its channels change with hal_sco() */
		pthread_mutex_lock(&b->hal_in_lock);
		uint32_t channels = b->in_channels;
		ssize_t n = b->in->read(b->in, hal, chunk * channels);
		pthread_mutex_unlock(&b->hal_in_lock);
		/* One microphone of the two: the upper one in a speakerphone conversation */
		if (n > 0) {
			uint32_t ch = channels == 2 && atomic_load(&b->speakerphone) ? 1 : 0;
			n /= channels;
			for (ssize_t s = 0; s < n / 2; s++)
				buf[s] = hal[s * channels + ch];
		}
		const void *data = buf;
		size_t size = n > 0 ? (size_t)n : 0;
		if (mic && size && atomic_load(&b->aec_on)) {
			size = echo_cancel(b, buf, size / 2, cancelled);
			data = cancelled;
		}
		/* The headset's source carries only the headset: silence until the HAL is on SCO, never
		 * the phone's microphone */
		const void *headset_data = atomic_load(&b->sco) ? (const void *)buf : (const void *)quiet;
		pthread_mutex_lock(&b->in_lock);
		if (n <= 0) {
			/* No data from the HAL (path not available): do not spin */
			pthread_mutex_unlock(&b->in_lock);
			struct timespec pause = {0, 20 * 1000000};
			nanosleep(&pause, NULL);
			pthread_mutex_lock(&b->in_lock);
			continue;
		}
		if (b->microphone.active)
			ring_write(&b->microphone, data, size);
		if (b->bt_mic.active)
			ring_write(&b->bt_mic, headset_data, n);
		for (size_t i = 0; i < SPA_N_ELEMENTS(sources); i++) {
			struct port *p = sources[i];
			while (ring_fill(p) >= p->period && p->active && !b->quit)
				if (!run_cycle(p))
					break; /* the graph did not run: try again after the next read */
		}
	}
	pthread_mutex_unlock(&b->in_lock);
	return NULL;
}

/* The FM source's HAL stream stays open this long after the last recording stopped: the app
 * reconnects when it changes its pipeline, and the FM path takes a moment to start */
#define FM_LINGER_MS 2000
/* After the HAL refused the FM input, the next attempt comes this much later */
#define FM_RETRY_MS 1000

static void add_ms(struct timespec *t, long ms)
{
	t->tv_sec += ms / 1000;
	t->tv_nsec += (ms % 1000) * 1000000;
	if (t->tv_nsec >= 1000000000) {
		t->tv_sec++;
		t->tv_nsec -= 1000000000;
	}
}

static struct audio_stream_in *open_fm(struct bridge *b)
{
	struct audio_config cfg = {.sample_rate = RATE, .channel_mask = AUDIO_CHANNEL_IN_STEREO,
				   .format = AUDIO_FORMAT_PCM_16_BIT};
	struct audio_stream_in *in = NULL;
	int err = b->dev->open_input_stream(b->dev, 3, AUDIO_DEVICE_IN_FM_TUNER, &cfg, &in, AUDIO_INPUT_FLAG_NONE,
					    NULL, AUDIO_SOURCE_FM_TUNER);
	if (err || !in) {
		fprintf(stderr, "audio-hal-bridge: no FM input (%d), the HAL suggests %u Hz, mask %#x, format %#x\n",
			err, cfg.sample_rate, cfg.channel_mask, cfg.format);
		return NULL;
	}
	if (cfg.sample_rate != RATE || cfg.channel_mask != AUDIO_CHANNEL_IN_STEREO || cfg.format != AUDIO_FORMAT_PCM_16_BIT) {
		fprintf(stderr, "audio-hal-bridge: FM input at %u Hz, mask %#x, format %#x: not taken\n",
			cfg.sample_rate, cfg.channel_mask, cfg.format);
		b->dev->close_input_stream(b->dev, in);
		return NULL;
	}
	pw_log_info("FM input open, HAL buffer %zu bytes", in->common.get_buffer_size(&in->common));
	return in;
}

/* The FM source: while it streams, read the HAL's FM input and run graph cycles as the capture
 * thread does. Without an FM path (the HAL refused it) it delivers silence in real time, so the
 * app's pipeline keeps running and the sound comes as soon as the path does. */
static void *fm_thread(void *data)
{
	struct bridge *b = data;
	struct port *p = &b->fm;
	static int16_t hal[CHUNK_MAX / 2];
	static const int16_t quiet[CHUNK_MAX / 2];
	struct timespec retry = {0, 0};

	pthread_mutex_lock(&b->fm_lock);
	while (!b->quit) {
		if (!p->active) {
			if (!b->fm_in) {
				spa_ringbuffer_init(&p->ring);
				pthread_cond_wait(&b->fm_cond, &b->fm_lock);
				continue;
			}
			struct timespec until;
			clock_gettime(CLOCK_REALTIME, &until);
			add_ms(&until, FM_LINGER_MS);
			while (!p->active && !b->quit)
				if (pthread_cond_timedwait(&b->fm_cond, &b->fm_lock, &until) == ETIMEDOUT)
					break;
			if (p->active || b->quit)
				continue;
			struct audio_stream_in *in = b->fm_in;
			b->fm_in = NULL;
			pthread_mutex_unlock(&b->fm_lock);
			b->dev->close_input_stream(b->dev, in);
			pw_log_info("FM input closed");
			pthread_mutex_lock(&b->fm_lock);
			continue;
		}
		pthread_mutex_unlock(&b->fm_lock);
		if (!b->fm_in) {
			struct timespec now;
			clock_gettime(CLOCK_MONOTONIC, &now);
			if (now.tv_sec > retry.tv_sec || (now.tv_sec == retry.tv_sec && now.tv_nsec >= retry.tv_nsec)) {
				b->fm_in = open_fm(b);
				if (!b->fm_in) {
					retry = now;
					add_ms(&retry, FM_RETRY_MS);
				}
			}
		}
		ssize_t n = b->fm_in ? b->fm_in->read(b->fm_in, hal, p->chunk) : -1;
		const void *data = hal;
		if (n <= 0) {
			struct timespec pause = {0, (long)(p->chunk / p->frame_size) * 1000000000L / RATE};
			nanosleep(&pause, NULL);
			data = quiet;
			n = (ssize_t)p->chunk;
		}
		pthread_mutex_lock(&b->fm_lock);
		if (p->active)
			ring_write(p, data, (uint32_t)n);
		while (ring_fill(p) >= p->period && p->active && !b->quit)
			if (!run_cycle(p))
				break;
	}
	struct audio_stream_in *in = b->fm_in;
	b->fm_in = NULL;
	pthread_mutex_unlock(&b->fm_lock);
	if (in)
		b->dev->close_input_stream(b->dev, in);
	return NULL;
}

static void on_process(void *data)
{
	struct port *p = data;
	struct pw_buffer *b = pw_stream_dequeue_buffer(p->stream);
	if (!b)
		return;
	struct spa_data *d = &b->buffer->datas[0];
	if (!d->data)
		goto done;

	pthread_mutex_lock(p->lock);
	if (p->playback) {
		uint32_t offset = SPA_MIN(d->chunk->offset, d->maxsize);
		uint32_t size = SPA_MIN(d->chunk->size, d->maxsize - offset);
		ring_write(p, SPA_PTROFF(d->data, offset, void), size);
		pthread_cond_signal(p->cond);
	} else {
		uint32_t want = d->maxsize;
		if (b->requested) {
			want = SPA_MIN((uint32_t)b->requested * p->frame_size, want);
			p->period = SPA_MIN(want, (uint32_t)CHUNK_MAX);
		}
		uint32_t got = ring_read(p, d->data, want);
		d->chunk->offset = 0;
		d->chunk->stride = p->frame_size;
		d->chunk->size = got;
		pthread_cond_signal(p->cond);
	}
	pthread_mutex_unlock(p->lock);
done:
	pw_stream_queue_buffer(p->stream, b);
}

static void on_state_changed(void *data, enum pw_stream_state old, enum pw_stream_state state, const char *error)
{
	struct port *p = data;
	pw_log_info("%s: %s -> %s%s%s", p->name, pw_stream_state_as_string(old), pw_stream_state_as_string(state),
		    error ? ": " : "", error ? error : "");
	pthread_mutex_lock(p->lock);
	p->active = state == PW_STREAM_STATE_STREAMING;
	pthread_cond_signal(p->cond);
	pthread_mutex_unlock(p->lock);
	/* The headphones start with the speaker's volume, every time they are plugged in (what
	 * WirePlumber restored for them is overwritten) */
	struct bridge *b = p->bridge;
	if (p == &b->phones && b->phones_fresh && state == PW_STREAM_STATE_PAUSED) {
		b->phones_fresh = false;
		float volume = b->speaker.volume, channel = atomic_load(&b->speaker.channel);
		float channels[2] = {channel, channel};
		pw_stream_set_control(p->stream, SPA_PROP_volume, 1, &volume,
				      SPA_PROP_channelVolumes, p->channels, channels, 0);
		pw_log_info("%s: the speaker's volume %.2f x %.2f", p->name, volume, channel);
	}
}

/* The call volume: in a mobile call the voice goes from the modem to the codec inside the device,
 * past PipeWire, and the HAL sets its level in the modem's speech path (MTK: setVoiceVolume →
 * SetDownlinkGain). Android's audio policy hands it the call volume index as index / max at the
 * start of every call and on each change; the HAL rounds volume * 7 to its 7 steps of
 * SpeechVol_AudioParam.xml (0.60 → step 3, 0.66 → step 4 of 0..6, measured 2026-11-02).
 * As on Android the call has a volume of its own, not the media's: during a call the sink the
 * volume keys change shows it (slider position = step / 7, as the cube root of the linear gain
 * PipeWire applies), each change moves one step at least, and afterwards the sink gets its media
 * volume back. Main thread. That is the way without role based volumes; with them the call is a
 * share of the output (apply_role_call_volume). */
#define CALL_STEPS 7
#define CALL_STEP_DEFAULT 6

static char *call_volume_path(void)
{
	return g_build_filename(g_get_user_state_dir(), "audio-hal-bridge", "call-volume", NULL);
}

static void load_call_volume(struct bridge *b)
{
	char *path = call_volume_path(), *text = NULL;
	b->call_step = CALL_STEP_DEFAULT;
	if (g_file_get_contents(path, &text, NULL, NULL)) {
		int step = atoi(text);
		if (step >= 1 && step <= CALL_STEPS)
			b->call_step = step;
	}
	g_free(text);
	g_free(path);
}

static void save_call_volume(struct bridge *b)
{
	char *path = call_volume_path(), *dir = g_path_get_dirname(path), text[16];
	snprintf(text, sizeof(text), "%d\n", b->call_step);
	if (g_mkdir_with_parents(dir, 0700) || !g_file_set_contents(path, text, -1, NULL))
		fprintf(stderr, "audio-hal-bridge: cannot save the call volume in %s\n", path);
	g_free(dir);
	g_free(path);
}

/* Sets a sink's channel volumes, as WirePlumber would */
static void set_sink_channel(struct bridge *b, struct port *p, float channel)
{
	float values[2] = {channel, channel};
	pw_thread_loop_lock(b->loop);
	if (p->stream) /* the headphones' sink goes away with the plug */
		pw_stream_set_control(p->stream, SPA_PROP_channelVolumes, p->channels, values, 0);
	pw_thread_loop_unlock(b->loop);
}

static void apply_call_volume(struct bridge *b, const char *why)
{
	float volume = (float)b->call_step / CALL_STEPS;
	pthread_mutex_lock(&b->route_lock);
	int ret = b->dev->set_voice_volume(b->dev, volume);
	pthread_mutex_unlock(&b->route_lock);
	fprintf(stderr, "audio-hal-bridge: call volume %d/%d (%s)%s\n", b->call_step, CALL_STEPS, why,
		ret ? " failed" : "");
}

/* With role based volumes the call is a share of the output like every role (the volume keys
 * change the output, the call channel sets the ratio): voice volume = output slider times call
 * channel slider. PipeWire shows a gain as its cube root, so the product of the sliders is the
 * slider of the product of the gains, which is what the role sink of a stream would give. The
 * keys' sink keeps its own volume through the call. Never below the first step: the keys cannot
 * silence the other side. A headset sets its own volume: then the call channel alone. */
static void apply_role_call_volume(struct bridge *b, float role_gain, bool start)
{
	struct port *p = atomic_load(&b->volume_port);
	float volume = cbrtf(role_gain) * (p ? cbrtf(atomic_load(&p->gain)) : 1.0f);
	volume = SPA_CLAMP(volume, 1.0f / CALL_STEPS, 1.0f);
	if (!start && fabsf(volume - b->voice_applied) < 0.005f)
		return;
	b->voice_applied = volume;
	pthread_mutex_lock(&b->route_lock);
	int ret = b->dev->set_voice_volume(b->dev, volume);
	pthread_mutex_unlock(&b->route_lock);
	fprintf(stderr, "audio-hal-bridge: call volume %.2f = output %s x call channel %.2f (%s)%s\n", volume,
		p ? p->name : "headset", cbrtf(role_gain), start ? "call start" : "change", ret ? " failed" : "");
}

static gboolean call_volume_update(gpointer data)
{
	struct bridge *b = data;
	atomic_store(&b->voice_scheduled, false);
	pthread_mutex_lock(&b->route_lock);
	bool call = b->call;
	pthread_mutex_unlock(&b->route_lock);

	float role_gain = atomic_load(&b->role_phone_gain);
	if (role_gain >= 0.0f && (call || b->call_on)) {
		/* Role based: the call channel came up during a call that showed the call volume on
		 * the keys' sink: that sink gets its own volume back */
		if (b->call_port) {
			set_sink_channel(b, b->call_port, b->media_channel);
			b->call_port = NULL;
		}
		bool start = call && !b->call_on;
		b->call_on = call;
		if (call)
			apply_role_call_volume(b, role_gain, start);
		return G_SOURCE_REMOVE;
	}

	if (call && !b->call_on) {
		/* The call starts: the keys' sink shows the call volume, unless they change a headset */
		struct port *p = atomic_load(&b->volume_port);
		b->call_on = true;
		b->call_port = p;
		if (p) {
			b->media_channel = atomic_load(&p->channel);
			float step = (float)b->call_step / CALL_STEPS;
			set_sink_channel(b, p, step * step * step);
		}
		apply_call_volume(b, "call start");
	} else if (call && b->call_port) {
		/* A change of the slider: one step at least, in its direction */
		struct port *p = b->call_port;
		float slider = cbrtf(atomic_load(&p->gain)) * CALL_STEPS;
		int step = b->call_step;
		if (slider > step + 0.05f)
			step = SPA_MAX(step + 1, (int)lroundf(slider));
		else if (slider < step - 0.05f)
			step = SPA_MIN(step - 1, (int)lroundf(slider));
		step = SPA_CLAMP(step, 1, CALL_STEPS);
		float wanted = (float)step / CALL_STEPS;
		if (fabsf(slider / CALL_STEPS - wanted) > 0.005f)
			set_sink_channel(b, p, wanted * wanted * wanted); /* the slider on the step */
		if (step != b->call_step) {
			b->call_step = step;
			apply_call_volume(b, "keys");
			save_call_volume(b);
		}
	} else if (!call && b->call_on) {
		/* The call ended: the media volume back */
		if (b->call_port)
			set_sink_channel(b, b->call_port, b->media_channel);
		b->call_port = NULL;
		b->call_on = false;
	}
	return G_SOURCE_REMOVE;
}

/* From any thread: the volume of a sink changed */
static void call_volume_schedule(struct bridge *b)
{
	if (!atomic_exchange(&b->voice_scheduled, true))
		g_idle_add(call_volume_update, b);
}

/* Volume changes of the node: PipeWire applies volume times the channel volume (the first
 * channel stands for all, balance aside) */
static void on_control_info(void *data, uint32_t id, const struct pw_stream_control *control)
{
	struct port *p = data;
	if (!p->playback || !control->n_values)
		return;
	if (id == SPA_PROP_volume)
		p->volume = control->values[0];
	else if (id == SPA_PROP_channelVolumes)
		p->channel_volume = control->values[0];
	else
		return;
	float gain = p->volume * p->channel_volume;
	atomic_store(&p->unvolume, gain > 0.001f ? 1.0f / gain : 0.0f);
	atomic_store(&p->gain, gain);
	atomic_store(&p->channel, p->channel_volume);
	call_volume_schedule(p->bridge);
}

static void on_trigger_done(void *data)
{
	struct port *p = data;
	pthread_mutex_lock(p->lock);
	p->cycles++;
	pthread_cond_signal(p->cond);
	pthread_mutex_unlock(p->lock);
}

static const struct pw_stream_events stream_events = {
	PW_VERSION_STREAM_EVENTS,
	.state_changed = on_state_changed,
	.control_info = on_control_info,
	.process = on_process,
	.trigger_done = on_trigger_done,
};

/* What the user sees of the HAL. Node names: plinius-health checks for hal_output, and the
 * WirePlumber hook of this package keeps alerts out of hal_earpiece. */
static void describe_ports(struct bridge *b)
{
	b->speaker = (struct port){.playback = true, .name = "hal_output", .description = "Speaker",
				   .icon = "audio-speakers", .priority = "2000", .channels = 2,
				   .unvolume = 1.0f, .volume = 1.0f, .channel_volume = 1.0f, .gain = 1.0f, .channel = 1.0f};
	b->earpiece = (struct port){.playback = true, .name = "hal_earpiece", .description = "Earpiece",
				    .icon = "phone", .priority = "1000", .channels = 2,
				    .unvolume = 1.0f, .volume = 1.0f, .channel_volume = 1.0f, .gain = 1.0f,
				    .channel = 1.0f};
	/* Above the speaker; WirePlumber makes it the default when it appears (new-device-default.lua) */
	b->phones = (struct port){.playback = true, .name = "hal_headphones", .description = "Headphones",
				      .icon = "audio-headphones", .priority = "2500", .channels = 2,
				      .unvolume = 1.0f, .volume = 1.0f, .channel_volume = 1.0f, .gain = 1.0f,
				      .channel = 1.0f};
	b->microphone = (struct port){.name = "hal_input", .description = "Microphone",
				      .icon = "audio-input-microphone", .priority = "2000", .channels = 1};
	b->speaker.lock = b->earpiece.lock = b->phones.lock = &b->out_lock;
	b->speaker.cond = b->earpiece.cond = b->phones.cond = &b->out_cond;
	b->microphone.lock = &b->in_lock;
	b->microphone.cond = &b->in_cond;
	/* Below the microphone: it becomes the default only when its headset connects (WirePlumber
	 * hook plinius/new-device-default.lua), not by itself */
	b->bt_mic = (struct port){.name = "hal_headset_input", .description = b->headset_name,
				       .icon = "audio-headset", .priority = "1500", .channels = 1};
	b->bt_mic.lock = &b->in_lock;
	b->bt_mic.cond = &b->in_cond;
	/* Far below the microphones: an app picks it by name, it is never the default source */
	b->fm = (struct port){.name = "hal_fm_input", .description = "FM Radio",
			      .icon = "audio-input-line", .priority = "100", .channels = 2};
	b->fm.lock = &b->fm_lock;
	b->fm.cond = &b->fm_cond;
}

/* The HAL's output on a device, 48 kHz stereo 16 bit; NULL if it offers something else */
static struct audio_stream_out *open_output(struct bridge *b, audio_devices_t device)
{
	struct audio_config cfg = {.sample_rate = RATE, .channel_mask = AUDIO_CHANNEL_OUT_STEREO,
				   .format = AUDIO_FORMAT_PCM_16_BIT};
	struct audio_stream_out *out = NULL;
	if (b->dev->open_output_stream(b->dev, 1, device, AUDIO_OUTPUT_FLAG_PRIMARY, &cfg, &out, NULL) || !out)
		return NULL;
	if (cfg.sample_rate != RATE || cfg.channel_mask != AUDIO_CHANNEL_OUT_STEREO || cfg.format != AUDIO_FORMAT_PCM_16_BIT) {
		b->dev->close_output_stream(b->dev, out);
		return NULL;
	}
	return out;
}

/* The HAL's input on a device at 48 kHz: stereo gives both microphones; mono (the lower one, or a
 * headset's) if the HAL has no stereo input there. Sets *channels. */
static struct audio_stream_in *open_input(struct bridge *b, audio_devices_t device, uint32_t *channels)
{
	static const audio_channel_mask_t masks[] = {AUDIO_CHANNEL_IN_STEREO, AUDIO_CHANNEL_IN_MONO};
	for (size_t i = 0; i < SPA_N_ELEMENTS(masks); i++) {
		struct audio_config cfg = {.sample_rate = RATE, .channel_mask = masks[i],
					   .format = AUDIO_FORMAT_PCM_16_BIT};
		struct audio_stream_in *in = NULL;
		if (b->dev->open_input_stream(b->dev, 2, device, &cfg, &in, AUDIO_INPUT_FLAG_NONE, NULL, AUDIO_SOURCE_MIC) ||
		    !in)
			continue;
		if (cfg.sample_rate != RATE || cfg.channel_mask != masks[i] || cfg.format != AUDIO_FORMAT_PCM_16_BIT) {
			b->dev->close_input_stream(b->dev, in);
			continue;
		}
		*channels = audio_channel_count_from_in_mask(masks[i]);
		return in;
	}
	return NULL;
}

static int open_hal(struct bridge *b)
{
	const hw_module_t *mod = NULL;
	if (hw_get_module_by_class(AUDIO_HARDWARE_MODULE_ID, AUDIO_HARDWARE_MODULE_ID_PRIMARY, &mod) || !mod) {
		fprintf(stderr, "audio-hal-bridge: no primary audio HAL\n");
		return -1;
	}
	if (mod->methods->open(mod, AUDIO_HARDWARE_INTERFACE, (struct hw_device_t **)&b->dev) || !b->dev ||
	    b->dev->init_check(b->dev)) {
		fprintf(stderr, "audio-hal-bridge: cannot open the audio HAL\n");
		return -1;
	}
	b->dev->set_mode(b->dev, AUDIO_MODE_NORMAL);

	b->out = open_output(b, AUDIO_DEVICE_OUT_SPEAKER);
	if (!b->out) {
		fprintf(stderr, "audio-hal-bridge: no 48 kHz stereo output stream\n");
		return -1;
	}
	b->speaker.chunk = b->earpiece.chunk = b->phones.chunk = b->out->common.get_buffer_size(&b->out->common);

	b->in = open_input(b, AUDIO_DEVICE_IN_BUILTIN_MIC, &b->in_channels);
	if (!b->in) {
		fprintf(stderr, "audio-hal-bridge: no 48 kHz input stream\n");
		return -1;
	}
	/* The node is mono: bytes per HAL transfer of the channel it takes */
	b->microphone.chunk = b->in->common.get_buffer_size(&b->in->common) / b->in_channels;
	b->bt_mic.chunk = b->microphone.chunk; /* fed from the same HAL reads */

	/* The FM input opens only while it is recorded: its transfers are taken as 1024 frames, the
	 * default below (the HAL's read() waits until it has them) */
	b->has_fm = access("/dev/fm", F_OK) == 0;
	b->fm.chunk = 0;

	struct port *ports[] = {&b->speaker, &b->earpiece, &b->phones, &b->microphone, &b->bt_mic, &b->fm};
	for (size_t i = 0; i < SPA_N_ELEMENTS(ports); i++) {
		struct port *p = ports[i];
		p->frame_size = p->channels * 2;
		if (p->chunk == 0 || p->chunk > CHUNK_MAX)
			p->chunk = 1024 * p->frame_size;
		p->chunk -= p->chunk % p->frame_size;
		p->period = p->chunk;
	}
	return 0;
}

static int connect_port(struct bridge *b, struct port *p)
{
	p->bridge = b;
	char latency[32], quantum[16], rate[16], media_name[64];
	snprintf(latency, sizeof(latency), "%zu/%u", p->chunk / p->frame_size, RATE);
	snprintf(quantum, sizeof(quantum), "%zu", p->chunk / p->frame_size);
	snprintf(rate, sizeof(rate), "%u", RATE);
	/* WirePlumber keeps the volume under media.name: "audio-hal-bridge speaker" since 0.1 */
	int len = snprintf(media_name, sizeof(media_name), "audio-hal-bridge %s", p->description);
	for (int i = 0; i < len && i < (int)sizeof(media_name); i++)
		media_name[i] = g_ascii_tolower(media_name[i]);
	struct pw_properties *props = pw_properties_new(
		PW_KEY_MEDIA_CLASS, p->playback ? "Audio/Sink" : "Audio/Source",
		PW_KEY_NODE_NAME, p->name,
		PW_KEY_NODE_DESCRIPTION, p->description,
		PW_KEY_DEVICE_ICON_NAME, p->icon,
		PW_KEY_NODE_LATENCY, latency,
		/* One graph cycle per HAL transfer, paced by the hardware like a sound card driver.
		 * With a smaller quantum two cycles would run back to back per transfer, and the
		 * apps missed the second one (xruns, lost audio: measured with pw-top) */
		PW_KEY_NODE_FORCE_QUANTUM, quantum,
		PW_KEY_NODE_FORCE_RATE, rate,
		"priority.session", p->priority,
		"priority.driver", "2000",
		NULL);
	if (p == &b->bt_mic || p == &b->phones) /* plinius/new-device-default.lua: a device that just connected */
		pw_properties_set(props, "audio-hal-bridge.headset", "true");
	/* No input to choose: the gvc of Settings and Phosh skips it (their patches), the radio
	 * app opens it by name through Pulse, plinius-callfocus counts it as no recording */
	if (p == &b->fm)
		pw_properties_set(props, "plinius.hidden", "true");
	spa_ringbuffer_init(&p->ring);
	p->stream = pw_stream_new_simple(pw_thread_loop_get_loop(b->loop), media_name, props, &stream_events, p);
	if (!p->stream)
		return -1;

	uint8_t buffer[1024];
	struct spa_pod_builder builder = SPA_POD_BUILDER_INIT(buffer, sizeof(buffer));
	struct spa_audio_info_raw info = SPA_AUDIO_INFO_RAW_INIT(.format = SPA_AUDIO_FORMAT_S16_LE,
								.channels = p->channels, .rate = RATE);
	if (p->channels == 2) {
		info.position[0] = SPA_AUDIO_CHANNEL_FL;
		info.position[1] = SPA_AUDIO_CHANNEL_FR;
	} else {
		info.position[0] = SPA_AUDIO_CHANNEL_MONO;
	}
	const struct spa_pod *params[1] = {spa_format_audio_raw_build(&builder, SPA_PARAM_EnumFormat, &info)};
	return pw_stream_connect(p->stream, p->playback ? PW_DIRECTION_INPUT : PW_DIRECTION_OUTPUT, PW_ID_ANY,
				 PW_STREAM_FLAG_MAP_BUFFERS | PW_STREAM_FLAG_DRIVER, params, 1);
}

/* A conversation in an app over the speaker: upper microphone, earpiece amplifier off. Not in a
 * mobile call, whose voice the modem's path handles. Call with route_lock held. */
static void update_speakerphone(struct bridge *b)
{
	bool on = atomic_load(&b->aec_on) && b->routed_out == AUDIO_DEVICE_OUT_SPEAKER && !b->call;
	if (on != atomic_load(&b->speakerphone)) {
		pw_log_info("speakerphone %s", on ? "on" : "off");
		atomic_store(&b->speakerphone, on);
	}
}

/* Routes the HAL streams by jack, call state and the sinks in use. Call with route_lock held. */
static void route(struct bridge *b)
{
	char value[64];
	audio_devices_t out, in;
	if (atomic_load(&b->sco)) {
		out = AUDIO_DEVICE_OUT_BLUETOOTH_SCO;
		in = AUDIO_DEVICE_IN_BLUETOOTH_SCO_HEADSET;
	} else {
		if (b->headphones)
			out = b->headset_mic ? AUDIO_DEVICE_OUT_WIRED_HEADSET : AUDIO_DEVICE_OUT_WIRED_HEADPHONE;
		else if (b->call ? !b->call_speaker : b->playing == SINK_EARPIECE)
			out = AUDIO_DEVICE_OUT_EARPIECE;
		else
			out = AUDIO_DEVICE_OUT_SPEAKER;
		in = b->headphones && b->headset_mic ? AUDIO_DEVICE_IN_WIRED_HEADSET : AUDIO_DEVICE_IN_BUILTIN_MIC;
	}
	snprintf(value, sizeof(value), "%s=%u", AUDIO_PARAMETER_STREAM_ROUTING, out);
	b->out->common.set_parameters(&b->out->common, value);
	snprintf(value, sizeof(value), "%s=%u", AUDIO_PARAMETER_STREAM_ROUTING, in);
	b->in->common.set_parameters(&b->in->common, value);
	b->routed_out = out;
	atomic_store(&b->speaker_routed, out == AUDIO_DEVICE_OUT_SPEAKER);
	update_speakerphone(b);
	pw_log_info("mode %s, output %#x, input %#x", b->call ? "call" : "normal", out, in);
}

/* The first input device whose name ends in "Headset Jack" (ASoC jack of the sound card) */
static int open_jack(void)
{
	DIR *dir = opendir("/dev/input");
	struct dirent *e;
	int fd = -1;
	while (dir && fd < 0 && (e = readdir(dir))) {
		char path[300], name[256] = "";
		if (strncmp(e->d_name, "event", 5))
			continue;
		snprintf(path, sizeof(path), "/dev/input/%s", e->d_name);
		fd = open(path, O_RDONLY | O_CLOEXEC);
		if (fd < 0)
			continue;
		ioctl(fd, EVIOCGNAME(sizeof(name)), name);
		size_t len = strlen(name);
		if (len < 12 || strcmp(name + len - 12, "Headset Jack")) {
			close(fd);
			fd = -1;
		}
	}
	if (dir)
		closedir(dir);
	return fd;
}

/* Tells the HAL that a wired device came or went, as Android's audio policy does before routing
 * to it: the HAL routes only to devices in its list of connected ones (mAvailableOutputDevices)
 * and only then sets the codec's "Headphone Plugged In". Without it, a stream routed to the
 * headphones opens no PCM at all and its writes just sleep (measured 2026-11-17 with a passive
 * USB-C adapter). Call with route_lock held. */
#ifndef AUDIO_PARAMETER_DEVICE_CONNECT
#define AUDIO_PARAMETER_DEVICE_CONNECT "connect"
#endif
#ifndef AUDIO_PARAMETER_DEVICE_DISCONNECT
#define AUDIO_PARAMETER_DEVICE_DISCONNECT "disconnect"
#endif

static void report_wired(struct bridge *b, audio_devices_t *reported, audio_devices_t now)
{
	char value[64];
	if (*reported == now)
		return;
	pthread_mutex_lock(&b->hal_out_lock);
	pthread_mutex_lock(&b->hal_in_lock);
	if (*reported) {
		snprintf(value, sizeof(value), "%s=%u", AUDIO_PARAMETER_DEVICE_DISCONNECT, *reported);
		b->dev->set_parameters(b->dev, value);
	}
	if (now) {
		snprintf(value, sizeof(value), "%s=%u", AUDIO_PARAMETER_DEVICE_CONNECT, now);
		b->dev->set_parameters(b->dev, value);
	}
	pthread_mutex_unlock(&b->hal_in_lock);
	pthread_mutex_unlock(&b->hal_out_lock);
	pw_log_info("wired device %#x -> %#x", *reported, now);
	*reported = now;
}

/* The "Headphones" sink while something is in the jack. Main thread: the streams are made and
 * destroyed under the PipeWire loop's lock, which the jack thread must not take while it holds
 * route_lock (the playback thread takes route_lock under out_lock, the PipeWire thread out_lock
 * under the loop's lock). */
static gboolean headphones_update(gpointer data)
{
	struct bridge *b = data;
	atomic_store(&b->phones_scheduled, false);
	pthread_mutex_lock(&b->route_lock);
	bool plugged = b->headphones;
	pthread_mutex_unlock(&b->route_lock);
	pw_thread_loop_lock(b->loop);
	if (plugged && !b->phones.stream) {
		b->phones_fresh = true;
		if (connect_port(b, &b->phones) < 0) {
			fprintf(stderr, "audio-hal-bridge: no headphones sink\n");
			if (b->phones.stream)
				pw_stream_destroy(b->phones.stream);
			b->phones.stream = NULL;
		}
	} else if (!plugged && b->phones.stream) {
		pw_stream_destroy(b->phones.stream);
		b->phones.stream = NULL;
		pthread_mutex_lock(&b->out_lock);
		b->phones.active = false;
		pthread_cond_signal(&b->out_cond);
		pthread_mutex_unlock(&b->out_lock);
	}
	pw_thread_loop_unlock(b->loop);
	return G_SOURCE_REMOVE;
}

static void jack_state(struct bridge *b)
{
	unsigned long sw[(SW_MAX + 8 * sizeof(long)) / (8 * sizeof(long))] = {0};
	ioctl(b->jack_fd, EVIOCGSW(sizeof(sw)), sw);
#define SW_BIT(n) ((sw[(n) / (8 * sizeof(long))] >> ((n) % (8 * sizeof(long)))) & 1)
	pthread_mutex_lock(&b->route_lock);
	b->headphones = SW_BIT(SW_HEADPHONE_INSERT) || SW_BIT(SW_LINEOUT_INSERT);
	b->headset_mic = SW_BIT(SW_MICROPHONE_INSERT);
	report_wired(b, &b->wired_out, !b->headphones ? 0
		: b->headset_mic ? AUDIO_DEVICE_OUT_WIRED_HEADSET : AUDIO_DEVICE_OUT_WIRED_HEADPHONE);
	report_wired(b, &b->wired_in, b->headphones && b->headset_mic ? AUDIO_DEVICE_IN_WIRED_HEADSET : 0);
	route(b);
	pthread_mutex_unlock(&b->route_lock);
	if (!atomic_exchange(&b->phones_scheduled, true))
		g_idle_add(headphones_update, b);
#undef SW_BIT
}

static void *jack_thread(void *data)
{
	struct bridge *b = data;
	struct input_event ev;
	jack_state(b);
	/* Re-read the whole state at each report (SYN_REPORT): plugs change several switches */
	while (read(b->jack_fd, &ev, sizeof(ev)) == sizeof(ev))
		if (ev.type == EV_SYN && ev.code == SYN_REPORT)
			jack_state(b);
	return NULL;
}

/* ---- Voice streams on the microphone: echo cancellation on or off ---- */

/* Roles of voice calls: "Communication" (PipeWire; pipewire-pulse maps PulseAudio's "phone",
 * e.g. GNOME Calls over SIP) and "Phone" */
static bool voice_role(const char *role)
{
	return role && (!strcasecmp(role, "Communication") || !strcasecmp(role, "Phone"));
}

/* Same app: the process if both are known, else the client */
static bool same_app(const struct stream_watch *a, const struct stream_watch *b)
{
	return *a->pid && *b->pid ? !strcmp(a->pid, b->pid) : a->client == b->client;
}

/* A conversation: the stream records with a call's role, or its app also plays */
static bool in_conversation(struct bridge *b, uint32_t node)
{
	for (size_t i = 0; i < VOICE_MAX; i++) {
		const struct stream_watch *s = &b->streams[i];
		if (!s->used || s->id != node || s->playback)
			continue;
		if (s->voice)
			return true;
		for (size_t j = 0; j < VOICE_MAX; j++)
			if (b->streams[j].used && b->streams[j].playback && same_app(&b->streams[j], s))
				return true;
	}
	return false;
}

/* Runs in the PipeWire thread, like the registry events */
static void voice_update(struct bridge *b)
{
	bool on = false;
	for (size_t l = 0; l < b->n_mic_links && !on; l++)
		on = in_conversation(b, b->mic_links[l].node);
	on = on && b->aec;
	if (on != atomic_load(&b->aec_on)) {
		pw_log_info("echo cancellation %s", on ? "on" : "off");
		atomic_store(&b->aec_on, on);
		pthread_mutex_lock(&b->route_lock);
		update_speakerphone(b);
		pthread_mutex_unlock(&b->route_lock);
	}
}

/* The full properties of an app stream: its process and role */
static void on_stream_info(void *data, const struct pw_node_info *info)
{
	struct stream_watch *s = data;
	if (!(info->change_mask & PW_NODE_CHANGE_MASK_PROPS) || !info->props)
		return;
	const char *pid = spa_dict_lookup(info->props, PW_KEY_APP_PROCESS_ID);
	snprintf(s->pid, sizeof(s->pid), "%s", pid ? pid : "");
	s->voice = s->voice || voice_role(spa_dict_lookup(info->props, PW_KEY_MEDIA_ROLE));
	voice_update(s->bridge);
}

static const struct pw_node_events stream_watch_events = {
	PW_VERSION_NODE_EVENTS,
	.info = on_stream_info,
};

/* ---- Bluetooth headsets: hands-free through the HAL (see the top) ---- */

static gboolean bt_update(gpointer data);

/* Bluetooth hands-free events go to the journal: rare, and what one needs to know afterwards */
#define bt_log(...) fprintf(stderr, "audio-hal-bridge: " __VA_ARGS__)

/* Queues bt_update() in the main loop, once (any thread) */
static void bt_schedule(struct bridge *b)
{
	if (!atomic_exchange(&b->bt_scheduled, true))
		g_idle_add(bt_update, b);
}

/* A node's profile is a hands-free one */
static bool hfp_profile(const char *profile)
{
	return !strncmp(profile, "headset-head-unit", 17);
}

/* A device's profile index is a hands-free one */
static bool hfp_profile_index(const struct bt_device *d, int index)
{
	return index >= 0 && (index == d->hfp || index == d->hfp_cvsd);
}

static struct bt_device *bt_device_by_id(struct bridge *b, uint32_t id)
{
	for (size_t i = 0; i < BT_MAX; i++)
		if (b->bt_devices[i].used && b->bt_devices[i].id == id)
			return &b->bt_devices[i];
	return NULL;
}

/* The device's sink node (A2DP or hands-free, the name stays) */
static struct bt_node *bt_sink_of(struct bridge *b, uint32_t device)
{
	for (size_t i = 0; i < SPA_N_ELEMENTS(b->bt_nodes); i++)
		if (b->bt_nodes[i].used && b->bt_nodes[i].sink && b->bt_nodes[i].device == device)
			return &b->bt_nodes[i];
	return NULL;
}

/* Profiles of a Bluetooth device: what it offers (EnumProfile) and the active one (Profile) */
static void on_bt_device_param(void *data, int seq, uint32_t id, uint32_t index, uint32_t next,
			       const struct spa_pod *param)
{
	struct bt_device *d = data;
	int32_t idx = -1;
	const char *name = NULL;
	if (spa_pod_parse_object(param, SPA_TYPE_OBJECT_ParamProfile, NULL, SPA_PARAM_PROFILE_index, SPA_POD_Int(&idx),
				 SPA_PARAM_PROFILE_name, SPA_POD_String(&name)) < 0 || !name)
		return;
	if (id == SPA_PARAM_EnumProfile) {
		/* Indices stay valid while the device is connected; an enumeration comes in pieces, so
		 * known ones are not forgotten in between */
		if (!strcmp(name, "headset-head-unit"))
			d->hfp = idx;
		else if (!strcmp(name, "headset-head-unit-cvsd"))
			d->hfp_cvsd = idx;
		else if (!strcmp(name, "a2dp-sink"))
			d->a2dp = idx;
	} else if (id == SPA_PARAM_Profile) {
		d->current = idx;
	}
	bt_schedule(d->bridge);
}

/* The device's profiles changed: ask for them again. A headset that just connected first offers
 * A2DP only, hands-free follows a few seconds later (measured: without this the headset's
 * source was missing after a reconnect). */
static void on_bt_device_info(void *data, const struct pw_device_info *info)
{
	struct bt_device *d = data;
	if (!(info->change_mask & PW_DEVICE_CHANGE_MASK_PARAMS) || !d->proxy)
		return;
	for (uint32_t i = 0; i < info->n_params; i++) {
		uint32_t id = info->params[i].id;
		if ((id == SPA_PARAM_EnumProfile || id == SPA_PARAM_Profile) && (info->params[i].flags & SPA_PARAM_INFO_READ))
			pw_device_enum_params((struct pw_device *)d->proxy, 0, id, 0, UINT32_MAX, NULL);
	}
}

static const struct pw_device_events bt_device_events = {
	PW_VERSION_DEVICE_EVENTS,
	.info = on_bt_device_info,
	.param = on_bt_device_param,
};

/* Profile and codec of a Bluetooth node */
static void on_bt_node_info(void *data, const struct pw_node_info *info)
{
	struct bt_node *n = data;
	if (!(info->change_mask & PW_NODE_CHANGE_MASK_PROPS) || !info->props)
		return;
	const char *v;
	if ((v = spa_dict_lookup(info->props, PW_KEY_DEVICE_ID)))
		n->device = atoi(v);
	v = spa_dict_lookup(info->props, PW_KEY_NODE_DESCRIPTION);
	snprintf(n->description, sizeof(n->description), "%s", v ? v : "");
	v = spa_dict_lookup(info->props, "api.bluez5.profile");
	snprintf(n->profile, sizeof(n->profile), "%s", v ? v : "");
	v = spa_dict_lookup(info->props, "api.bluez5.codec");
	snprintf(n->codec, sizeof(n->codec), "%s", v ? v : "");
	bt_schedule(n->bridge);
}

static const struct pw_node_events bt_node_events = {
	PW_VERSION_NODE_EVENTS,
	.info = on_bt_node_info,
};

/* The default sink, as WirePlumber decided it */
static int on_metadata_property(void *data, uint32_t subject, const char *key, const char *type, const char *value)
{
	struct bridge *b = data;
	/* Only the global entries (subject 0): when PipeWire removes a node it clears that node's
	 * entries with key NULL, which once emptied the default sink here, so the next call went to
	 * the earpiece instead of the connected headset (2026-11-02) */
	if (subject != PW_ID_CORE || (key && strcmp(key, "default.audio.sink")))
		return 0;
	b->default_sink[0] = 0;
	if (value && spa_json_str_object_find(value, strlen(value), "name", b->default_sink, sizeof(b->default_sink)) <= 0)
		b->default_sink[0] = 0;
	atomic_store(&b->volume_port, !strcmp(b->default_sink, "hal_earpiece")     ? &b->earpiece
				      : !strcmp(b->default_sink, "hal_headphones") ? &b->phones
				      : !strcmp(b->default_sink, "hal_output") || !*b->default_sink ? &b->speaker
				      : NULL);
	call_volume_schedule(b);
	bt_schedule(b);
	return 0;
}

static const struct pw_metadata_events metadata_events = {
	PW_VERSION_METADATA_EVENTS,
	.property = on_metadata_property,
};

/* The call channel's volume: its role sink's Props, volume times the channel volume (the first
 * channel stands for all, as for the HAL sinks) */
static void on_role_phone_param(void *data, int seq, uint32_t id, uint32_t index, uint32_t next,
				const struct spa_pod *param)
{
	struct bridge *b = data;
	if (id != SPA_PARAM_Props || !param || !spa_pod_is_object(param))
		return;
	float volume = 1.0f, channels[SPA_AUDIO_MAX_CHANNELS];
	uint32_t n = 0;
	bool found = false;
	const struct spa_pod_prop *prop;
	SPA_POD_OBJECT_FOREACH((const struct spa_pod_object *)param, prop) {
		if (prop->key == SPA_PROP_volume && spa_pod_get_float(&prop->value, &volume) == 0)
			found = true;
		else if (prop->key == SPA_PROP_channelVolumes &&
			 (n = spa_pod_copy_array(&prop->value, SPA_TYPE_Float, channels, SPA_N_ELEMENTS(channels))))
			found = true;
	}
	if (!found)
		return; /* Props of another kind, without volumes */
	atomic_store(&b->role_phone_gain, volume * (n ? channels[0] : 1.0f));
	call_volume_schedule(b);
}

static const struct pw_node_events role_phone_events = {
	PW_VERSION_NODE_EVENTS,
	.param = on_role_phone_param,
};

/* Registry: Bluetooth audio devices and nodes, the default metadata. Returns true if taken. */
static bool bt_global(struct bridge *b, uint32_t id, const char *type, const struct spa_dict *props)
{
	if (!strcmp(type, PW_TYPE_INTERFACE_Device)) {
		const char *api = spa_dict_lookup(props, PW_KEY_DEVICE_API);
		const char *class = spa_dict_lookup(props, PW_KEY_MEDIA_CLASS);
		if (!api || strcmp(api, "bluez5") || !class || strcmp(class, "Audio/Device"))
			return false;
		struct bt_device *d = NULL;
		for (size_t i = 0; i < BT_MAX && !d; i++)
			if (!b->bt_devices[i].used)
				d = &b->bt_devices[i];
		if (!d)
			return true;
		*d = (struct bt_device){.bridge = b, .used = true, .id = id, .hfp = -1, .hfp_cvsd = -1, .a2dp = -1,
					.current = -1};
		d->proxy = pw_registry_bind(b->registry, id, type, PW_VERSION_DEVICE, 0);
		if (d->proxy) {
			pw_device_add_listener((struct pw_device *)d->proxy, &d->listener, &bt_device_events, d);
			uint32_t ids[] = {SPA_PARAM_EnumProfile, SPA_PARAM_Profile};
			pw_device_subscribe_params((struct pw_device *)d->proxy, ids, SPA_N_ELEMENTS(ids));
		}
		return true;
	}
	if (!strcmp(type, PW_TYPE_INTERFACE_Node)) {
		const char *name = spa_dict_lookup(props, PW_KEY_NODE_NAME);
		const char *class = spa_dict_lookup(props, PW_KEY_MEDIA_CLASS);
		if (name && !strcmp(name, "input.loopback.sink.role.phone")) {
			if (!b->role_phone) {
				b->role_phone = pw_registry_bind(b->registry, id, type, PW_VERSION_NODE, 0);
				b->role_phone_id = id;
				if (b->role_phone) {
					uint32_t ids[] = {SPA_PARAM_Props};
					pw_node_add_listener((struct pw_node *)b->role_phone, &b->role_phone_listener,
							     &role_phone_events, b);
					pw_node_subscribe_params((struct pw_node *)b->role_phone, ids, SPA_N_ELEMENTS(ids));
				}
			}
			return true;
		}
		bool sink = name && !strncmp(name, "bluez_output.", 13);
		if (!sink && !(name && !strncmp(name, "bluez_input.", 12)))
			return false;
		if (!class || (strcmp(class, "Audio/Sink") && strcmp(class, "Audio/Source")))
			return true; /* the inner stream of the headset microphone's loopback */
		struct bt_node *n = NULL;
		for (size_t i = 0; i < SPA_N_ELEMENTS(b->bt_nodes) && !n; i++)
			if (!b->bt_nodes[i].used)
				n = &b->bt_nodes[i];
		if (!n)
			return true;
		*n = (struct bt_node){.bridge = b, .used = true, .sink = sink, .id = id};
		snprintf(n->name, sizeof(n->name), "%s", name);
		n->proxy = pw_registry_bind(b->registry, id, type, PW_VERSION_NODE, 0);
		if (n->proxy)
			pw_node_add_listener((struct pw_node *)n->proxy, &n->listener, &bt_node_events, n);
		return true;
	}
	if (!strcmp(type, PW_TYPE_INTERFACE_Metadata)) {
		const char *name = spa_dict_lookup(props, PW_KEY_METADATA_NAME);
		if (!name || strcmp(name, "default") || b->metadata)
			return true;
		b->metadata = pw_registry_bind(b->registry, id, type, PW_VERSION_METADATA, 0);
		if (b->metadata)
			pw_metadata_add_listener((struct pw_metadata *)b->metadata, &b->metadata_listener, &metadata_events, b);
		return true;
	}
	return false;
}

static void bt_global_remove(struct bridge *b, uint32_t id)
{
	for (size_t i = 0; i < BT_MAX; i++) {
		struct bt_device *d = &b->bt_devices[i];
		if (d->used && d->id == id) {
			if (d->proxy) {
				spa_hook_remove(&d->listener);
				pw_proxy_destroy(d->proxy);
			}
			d->used = false;
			bt_schedule(b);
		}
	}
	for (size_t i = 0; i < SPA_N_ELEMENTS(b->bt_nodes); i++) {
		struct bt_node *n = &b->bt_nodes[i];
		if (n->used && n->id == id) {
			if (n->proxy) {
				spa_hook_remove(&n->listener);
				pw_proxy_destroy(n->proxy);
			}
			n->used = false;
			bt_schedule(b);
		}
	}
	if (b->metadata && pw_proxy_get_bound_id(b->metadata) == id) {
		spa_hook_remove(&b->metadata_listener);
		pw_proxy_destroy(b->metadata);
		b->metadata = NULL;
		b->default_sink[0] = 0;
	}
	if (b->role_phone && b->role_phone_id == id) {
		/* WirePlumber went away with its role sinks: the call volume of its own again */
		spa_hook_remove(&b->role_phone_listener);
		pw_proxy_destroy(b->role_phone);
		b->role_phone = NULL;
		atomic_store(&b->role_phone_gain, -1.0f);
		call_volume_schedule(b);
	}
}

/* Asks the device for a profile, not remembered by WirePlumber. Call with the loop locked. */
static void bt_set_profile(struct bt_device *d, int index)
{
	uint8_t buffer[256];
	struct spa_pod_builder builder = SPA_POD_BUILDER_INIT(buffer, sizeof(buffer));
	const struct spa_pod *param = spa_pod_builder_add_object(&builder, SPA_TYPE_OBJECT_ParamProfile, SPA_PARAM_Profile,
		SPA_PARAM_PROFILE_index, SPA_POD_Int(index), SPA_PARAM_PROFILE_save, SPA_POD_Bool(false));
	bt_log("bluetooth device %u: profile %d\n", d->id, index);
	pw_device_set_param((struct pw_device *)d->proxy, SPA_PARAM_Profile, 0, param);
}

/* The hold stream: silence to the headset's hands-free sink. While a stream is active on it,
 * PipeWire keeps the SCO link up; the audio itself goes through the HAL. */
static void on_sco_hold_process(void *data)
{
	struct bridge *b = data;
	struct pw_buffer *buf = pw_stream_dequeue_buffer(b->sco_hold);
	if (!buf)
		return;
	struct spa_data *d = &buf->buffer->datas[0];
	if (d->data) {
		uint32_t size = buf->requested ? SPA_MIN((uint32_t)buf->requested * 2, d->maxsize) : d->maxsize;
		memset(d->data, 0, size);
		d->chunk->offset = 0;
		d->chunk->stride = 2;
		d->chunk->size = size;
	}
	pw_stream_queue_buffer(b->sco_hold, buf);
}

static void on_sco_hold_state(void *data, enum pw_stream_state old, enum pw_stream_state state, const char *error)
{
	struct bridge *b = data;
	b->sco_hold_streaming = state == PW_STREAM_STATE_STREAMING;
	bt_schedule(b);
}

static const struct pw_stream_events sco_hold_events = {
	PW_VERSION_STREAM_EVENTS,
	.state_changed = on_sco_hold_state,
	.process = on_sco_hold_process,
};

/* Call with the loop locked */
static void sco_hold_start(struct bridge *b, const char *sink)
{
	struct pw_properties *props = pw_properties_new(
		PW_KEY_MEDIA_TYPE, "Audio",
		PW_KEY_MEDIA_CATEGORY, "Playback",
		PW_KEY_NODE_NAME, "audio-hal-bridge.sco-hold", /* sco-to-hal.lua leaves it on the headset */
		PW_KEY_TARGET_OBJECT, sink, /* the name stays when the profile changes */
		/* Only the headset holds the link. Without a role of its own the hold got the default
		 * role "Multimedia" (55-plinius-media-roles.conf), and a target WirePlumber had
		 * stored for it (target.node -1) sent it into the multimedia role sink and on to the
		 * HAL: the hands-free sink stayed idle, the SCO link went down within a second, and
		 * calls lost the headset's microphone after the first word (measured 2026-11-18). */
		PW_KEY_MEDIA_ROLE, "Hold",
		"node.dont-move", "true",
		NULL);
	b->sco_hold_streaming = false;
	b->sco_hold = pw_stream_new_simple(pw_thread_loop_get_loop(b->loop), "audio-hal-bridge SCO hold", props,
					   &sco_hold_events, b);
	if (!b->sco_hold)
		return;
	uint8_t buffer[512];
	struct spa_pod_builder builder = SPA_POD_BUILDER_INIT(buffer, sizeof(buffer));
	struct spa_audio_info_raw info = SPA_AUDIO_INFO_RAW_INIT(.format = SPA_AUDIO_FORMAT_S16_LE, .channels = 1,
								.rate = 16000, .position = {SPA_AUDIO_CHANNEL_MONO});
	const struct spa_pod *params[1] = {spa_format_audio_raw_build(&builder, SPA_PARAM_EnumFormat, &info)};
	pw_stream_connect(b->sco_hold, PW_DIRECTION_OUTPUT, PW_ID_ANY,
			  PW_STREAM_FLAG_AUTOCONNECT | PW_STREAM_FLAG_MAP_BUFFERS, params, 1);
}

/* Call with the loop locked */
static void sco_hold_stop(struct bridge *b)
{
	if (b->sco_hold)
		pw_stream_destroy(b->sco_hold);
	b->sco_hold = NULL;
	b->sco_hold_streaming = false;
}

/* Puts both HAL streams on Bluetooth SCO, or back. The HAL picks its PCM device while opening a
 * stream (BTCVSD for Bluetooth), and BT_SCO and bt_wbs have to reach it before: a route set on
 * an open stream leaves the sound where it was (furios_audio, and here: the probe opened
 * pcmC0D74 = BTCVSD only that way). Main thread, loop unlocked. */
static void hal_sco(struct bridge *b, bool on, bool wbs)
{
	pthread_mutex_lock(&b->route_lock);
	pthread_mutex_lock(&b->hal_out_lock);
	pthread_mutex_lock(&b->hal_in_lock);
	b->dev->close_output_stream(b->dev, b->out);
	b->dev->close_input_stream(b->dev, b->in);
	b->out = NULL;
	b->in = NULL;
	if (on) {
		b->dev->set_parameters(b->dev, wbs ? "bt_wbs=on" : "bt_wbs=off");
		b->dev->set_parameters(b->dev, "BT_SCO=on");
		b->out = open_output(b, AUDIO_DEVICE_OUT_BLUETOOTH_SCO);
		b->in = b->out ? open_input(b, AUDIO_DEVICE_IN_BLUETOOTH_SCO_HEADSET, &b->in_channels) : NULL;
		if (!b->in) {
			fprintf(stderr, "audio-hal-bridge: no HAL streams on Bluetooth SCO, staying on the phone\n");
			if (b->out)
				b->dev->close_output_stream(b->dev, b->out);
			b->out = NULL;
			on = false;
		}
	}
	if (!on) {
		b->dev->set_parameters(b->dev, "BT_SCO=off");
		b->out = open_output(b, AUDIO_DEVICE_OUT_SPEAKER);
		b->in = open_input(b, AUDIO_DEVICE_IN_BUILTIN_MIC, &b->in_channels);
		if (!b->out || !b->in) {
			/* Without streams the bridge cannot go on: the service restarts it */
			fprintf(stderr, "audio-hal-bridge: cannot reopen the HAL streams\n");
			abort();
		}
	}
	atomic_store(&b->sco, on);
	bt_log("bluetooth sco %s%s, input %u channels\n", on ? "on" : "off", on ? (wbs ? " (wide band)" : " (narrow band)") : "",
		    b->in_channels);
	route(b); /* the routing of the new streams, and call mode as it is */
	pthread_mutex_unlock(&b->hal_in_lock);
	pthread_mutex_unlock(&b->hal_out_lock);
	pthread_mutex_unlock(&b->route_lock);
}

/* Ends hands-free: phone streams, no hold, the headset back in its earlier profile. Main
 * thread, loop locked (unlocked around the HAL). */
static void sco_stop(struct bridge *b, const char *why)
{
	if (b->sco_timeout)
		g_source_remove(b->sco_timeout);
	if (b->sco_release)
		g_source_remove(b->sco_release);
	b->sco_timeout = b->sco_release = 0;
	bt_log("bluetooth hands-free off: %s\n", why);
	if (atomic_load(&b->sco)) {
		pw_thread_loop_unlock(b->loop);
		hal_sco(b, false, false);
		pw_thread_loop_lock(b->loop);
	}
	sco_hold_stop(b);
	/* Back to where it was, unless it was in hands-free before (somebody chose that). Always
	 * asked: the device's report of its active profile may lag behind. */
	struct bt_device *d = bt_device_by_id(b, b->sco_device);
	if (d && d->proxy && b->sco_restore >= 0)
		bt_set_profile(d, b->sco_restore);
	b->sco_state = SCO_OFF;
	b->sco_codec[0] = 0;
}

static gboolean on_sco_timeout(gpointer data)
{
	struct bridge *b = data;
	b->sco_timeout = 0;
	pw_thread_loop_lock(b->loop);
	if (b->sco_state == SCO_SWITCHING) {
		sco_stop(b, "the headset did not take hands-free");
		b->sco_blocked = true;
	}
	pw_thread_loop_unlock(b->loop);
	return G_SOURCE_REMOVE;
}

static gboolean on_sco_release(gpointer data)
{
	struct bridge *b = data;
	b->sco_release = 0;
	pw_thread_loop_lock(b->loop);
	sco_stop(b, "no call or recording any more");
	pw_thread_loop_unlock(b->loop);
	return G_SOURCE_REMOVE;
}

/* A device that can do hands-free */
static bool bt_can_hfp(const struct bt_device *d)
{
	return d && d->proxy && (d->hfp >= 0 || d->hfp_cvsd >= 0 || hfp_profile_index(d, d->current));
}

/* The headset's microphone as a source of the bridge, under the headset's name, while a headset
 * that can do hands-free is connected: the user sees one input per device, the profiles stay
 * behind it. Call with the loop locked. */
static void headset_source_update(struct bridge *b, struct bt_device *preferred)
{
	/* It stays while its device is connected: a profile change replaces the device's nodes for
	 * a moment, and the source going away with them would end the very recording that asked
	 * for hands-free */
	struct bt_device *d = b->bt_mic.stream ? bt_device_by_id(b, b->headset_device) : NULL;
	if (b->bt_mic.stream && !bt_can_hfp(d)) {
		bt_log("headset source %s removed\n", b->headset_name);
		pw_stream_destroy(b->bt_mic.stream);
		b->bt_mic.stream = NULL;
		pthread_mutex_lock(&b->in_lock);
		b->bt_mic.active = false;
		pthread_mutex_unlock(&b->in_lock);
		b->n_headset_links = 0;
	}
	if (b->bt_mic.stream)
		return;
	/* The headset of the default sink first, else any that can do hands-free */
	d = bt_can_hfp(preferred) ? preferred : NULL;
	for (size_t i = 0; i < BT_MAX && !d; i++)
		if (b->bt_devices[i].used && bt_can_hfp(&b->bt_devices[i]) && bt_sink_of(b, b->bt_devices[i].id))
			d = &b->bt_devices[i];
	struct bt_node *sink = d ? bt_sink_of(b, d->id) : NULL;
	if (!sink || !*sink->description)
		return;
	snprintf(b->headset_name, sizeof(b->headset_name), "%s", sink->description);
	b->headset_device = d->id;
	if (connect_port(b, &b->bt_mic) < 0) {
		if (b->bt_mic.stream)
			pw_stream_destroy(b->bt_mic.stream);
		b->bt_mic.stream = NULL;
		return;
	}
	bt_log("headset source %s for %s\n", b->headset_name, sink->name);
}

/* The hands-free state machine, after every change of the call, the headset source's users, the
 * headset's profiles or nodes, or the default sink. Main thread. */
static gboolean bt_update(gpointer data)
{
	struct bridge *b = data;
	atomic_store(&b->bt_scheduled, false);
	pthread_mutex_lock(&b->route_lock);
	bool call = b->call && !b->call_speaker;
	pthread_mutex_unlock(&b->route_lock);

	pw_thread_loop_lock(b->loop);
	/* The headset of the default sink: a call goes there */
	struct bt_device *output = NULL;
	for (size_t i = 0; i < SPA_N_ELEMENTS(b->bt_nodes) && !output; i++) {
		struct bt_node *n = &b->bt_nodes[i];
		if (n->used && n->sink && n->device && !strcmp(n->name, b->default_sink))
			output = bt_device_by_id(b, n->device);
	}
	headset_source_update(b, output);
	/* An app records from the headset's source: a stream of an app, not the inner stream of a
	 * filter or loopback (WirePlumber's own headset loopback links to it in hands-free) */
	bool recording = false;
	for (size_t l = 0; b->bt_mic.stream && l < b->n_headset_links && !recording; l++)
		for (size_t i = 0; i < VOICE_MAX && !recording; i++)
			recording = b->streams[i].used && !b->streams[i].playback &&
				    b->streams[i].id == b->headset_links[l].node;
	/* The headset: the one in hands-free already, else the one a call or a recording needs */
	struct bt_device *d = NULL;
	if (b->sco_state != SCO_OFF)
		d = bt_device_by_id(b, b->sco_device);
	else if (call && bt_can_hfp(output))
		d = output;
	else if (recording)
		d = bt_device_by_id(b, b->headset_device);
	bool want = bt_can_hfp(d) && ((call && d == output) || (recording && d->id == b->headset_device));
	if (want)
		call = call && d == output; /* for the log: what it is for */
	if (!want)
		b->sco_blocked = false;
	struct bt_node *sink = d ? bt_sink_of(b, d->id) : NULL;
	{
		/* What the decision saw, logged when it changes */
		static char last[512];
		char now[512];
		snprintf(now, sizeof(now),
			 "bt state: call %d, default %s, output %d (hfp %d/%d current %d), recording %d, device %d, want %d, "
			 "blocked %d, sink %s (%s, %s), hold %d/%d, sco %d",
			 call, b->default_sink, output ? (int)output->id : -1, output ? output->hfp : -1,
			 output ? output->hfp_cvsd : -1, output ? output->current : -1, recording, d ? (int)d->id : -1, want,
			 b->sco_blocked, sink ? sink->name : "-", sink ? sink->profile : "-", sink ? sink->codec : "-",
			 b->sco_hold != NULL, b->sco_hold_streaming, b->sco_state);
		if (strcmp(now, last)) {
			bt_log("%s\n", now);
			snprintf(last, sizeof(last), "%s", now);
		}
	}

	switch (b->sco_state) {
	case SCO_OFF:
		if (!want || b->sco_blocked || !sink)
			break;
		bt_log("bluetooth hands-free on for %s (%s)\n", sink->name, call ? "call" : "recording");
		b->sco_device = d->id;
		b->sco_restore = hfp_profile_index(d, d->current) ? -1 : d->current >= 0 ? d->current : d->a2dp;
		/* By the device's profile, not the node's: a node may still show hands-free while the
		 * device goes back to A2DP (measured 2026-11-02) */
		if (!hfp_profile_index(d, d->current) || !hfp_profile(sink->profile))
			bt_set_profile(d, d->hfp >= 0 ? d->hfp : d->hfp_cvsd);
		b->sco_state = SCO_SWITCHING;
		b->sco_timeout = g_timeout_add(SCO_SWITCH_MS, on_sco_timeout, b);
		/* A device in hands-free already (a car that connects with HFP only) changes nothing
		 * that would call bt_update() again: go on with the switch from here, or the call waited
		 * for the timeout and stayed on the phone (2026-11-03) */
		bt_schedule(b);
		break;
	case SCO_SWITCHING:
		/* While the profile changes PipeWire replaces the headset's nodes, and for a moment the
		 * default sink has no device: in a call that is no reason to give up (that ended every
		 * call on a headset, 2026-11-02); the timeout ends a switch that does not come */
		if (!want && call && d && !output)
			break;
		if (!want) {
			sco_stop(b, d ? "not needed any more" : "the headset went away");
			break;
		}
		/* The hold goes to the hands-free sink only: started on the A2DP sink it would stay
		 * behind when the profile changes */
		if (!sink || !hfp_profile(sink->profile))
			break;
		if (!b->sco_hold)
			sco_hold_start(b, sink->name);
		/* Ready when the codec is agreed and the hold runs */
		if (!*sink->codec || !b->sco_hold_streaming)
			break;
		if (strcmp(sink->codec, "msbc") && strcmp(sink->codec, "cvsd")) {
			/* A codec the HAL has no word for (LC3-SWB): narrow band is understood by all */
			if (d->hfp_cvsd >= 0 && d->current != d->hfp_cvsd)
				bt_set_profile(d, d->hfp_cvsd);
			else
				sco_stop(b, "no codec the HAL can code");
			break;
		}
		g_source_remove(b->sco_timeout);
		b->sco_timeout = 0;
		snprintf(b->sco_codec, sizeof(b->sco_codec), "%s", sink->codec);
		b->sco_state = SCO_ON;
		pw_thread_loop_unlock(b->loop);
		hal_sco(b, true, !strcmp(b->sco_codec, "msbc"));
		pw_thread_loop_lock(b->loop);
		break;
	case SCO_ON:
		if (!d) {
			sco_stop(b, "the headset went away");
			break;
		}
		if (!want) {
			if (!b->sco_release)
				b->sco_release = g_timeout_add(SCO_RELEASE_MS, on_sco_release, b);
			break;
		}
		if (b->sco_release) {
			g_source_remove(b->sco_release);
			b->sco_release = 0;
		}
		/* The codec is agreed again when the link comes back up: tell the HAL if it changed */
		if (sink && hfp_profile(sink->profile) && *sink->codec && strcmp(sink->codec, b->sco_codec) &&
		    (!strcmp(sink->codec, "msbc") || !strcmp(sink->codec, "cvsd"))) {
			snprintf(b->sco_codec, sizeof(b->sco_codec), "%s", sink->codec);
			pw_thread_loop_unlock(b->loop);
			hal_sco(b, true, !strcmp(b->sco_codec, "msbc"));
			pw_thread_loop_lock(b->loop);
		}
		break;
	}
	pw_thread_loop_unlock(b->loop);
	return G_SOURCE_REMOVE;
}

static void on_global(void *data, uint32_t id, uint32_t permissions, const char *type, uint32_t version,
		      const struct spa_dict *props)
{
	struct bridge *b = data;
	if (!props)
		return;
	if (bt_global(b, id, type, props))
		return;
	if (!strcmp(type, PW_TYPE_INTERFACE_Node)) {
		const char *class = spa_dict_lookup(props, PW_KEY_MEDIA_CLASS);
		const char *client = spa_dict_lookup(props, PW_KEY_CLIENT_ID);
		bool playback = class && !strcmp(class, "Stream/Output/Audio");
		/* App streams only, not the inner streams of filters and loopbacks (a link group) */
		if (!client || spa_dict_lookup(props, PW_KEY_NODE_LINK_GROUP) ||
		    (!playback && (!class || strcmp(class, "Stream/Input/Audio"))))
			return;
		struct stream_watch *s = NULL;
		for (size_t i = 0; i < VOICE_MAX && !s; i++)
			if (!b->streams[i].used)
				s = &b->streams[i];
		if (!s)
			return;
		*s = (struct stream_watch){.bridge = b, .used = true, .id = id, .client = atoi(client),
					   .playback = playback,
					   .voice = voice_role(spa_dict_lookup(props, PW_KEY_MEDIA_ROLE))};
		s->proxy = pw_registry_bind(b->registry, id, type, PW_VERSION_NODE, 0);
		if (s->proxy)
			pw_node_add_listener((struct pw_node *)s->proxy, &s->listener, &stream_watch_events, s);
	} else if (!strcmp(type, PW_TYPE_INTERFACE_Link)) {
		const char *out = spa_dict_lookup(props, PW_KEY_LINK_OUTPUT_NODE);
		const char *in = spa_dict_lookup(props, PW_KEY_LINK_INPUT_NODE);
		if (out && in && b->bt_mic.stream &&
		    (uint32_t)atoi(out) == pw_stream_get_node_id(b->bt_mic.stream)) {
			if (b->n_headset_links < VOICE_MAX) {
				b->headset_links[b->n_headset_links].link = id;
				b->headset_links[b->n_headset_links++].node = atoi(in);
			}
			bt_schedule(b); /* a recording from the headset starts */
			return;
		}
		if (!out || !in || (uint32_t)atoi(out) != pw_stream_get_node_id(b->microphone.stream) ||
		    b->n_mic_links == VOICE_MAX)
			return;
		b->mic_links[b->n_mic_links].link = id;
		b->mic_links[b->n_mic_links++].node = atoi(in);
	} else {
		return;
	}
	voice_update(b);
	bt_schedule(b); /* a recording may start */
}

static void on_global_remove(void *data, uint32_t id)
{
	struct bridge *b = data;
	/* Unordered lists: the last entry takes the place of a removed one */
	for (size_t i = 0; i < VOICE_MAX; i++) {
		struct stream_watch *s = &b->streams[i];
		if (!s->used || s->id != id)
			continue;
		if (s->proxy) {
			spa_hook_remove(&s->listener);
			pw_proxy_destroy(s->proxy);
		}
		s->used = false;
	}
	for (size_t i = 0; i < b->n_mic_links;)
		if (b->mic_links[i].link == id)
			b->mic_links[i] = b->mic_links[--b->n_mic_links];
		else
			i++;
	for (size_t i = 0; i < b->n_headset_links;)
		if (b->headset_links[i].link == id)
			b->headset_links[i] = b->headset_links[--b->n_headset_links];
		else
			i++;
	bt_global_remove(b, id);
	voice_update(b);
	bt_schedule(b); /* a recording may have ended */
}

static const struct pw_registry_events registry_events = {
	PW_VERSION_REGISTRY_EVENTS,
	.global = on_global,
	.global_remove = on_global_remove,
};

/* Follows the registry for voice streams on the microphone. Call with the loop locked. */
static void watch_voice(struct bridge *b)
{
	b->context = pw_context_new(pw_thread_loop_get_loop(b->loop), NULL, 0);
	b->core = b->context ? pw_context_connect(b->context, NULL, 0) : NULL;
	b->registry = b->core ? pw_core_get_registry(b->core, PW_VERSION_REGISTRY, 0) : NULL;
	if (!b->registry) {
		fprintf(stderr, "audio-hal-bridge: no registry, no echo cancellation\n");
		return;
	}
	pw_registry_add_listener(b->registry, &b->registry_listener, &registry_events, b);
}

/* ---- org.mobian_project.CallAudio (callaudiod's API) ---- */

/* The HAL leaves its input streams suspended after a call: MTK's AudioALSAStreamIn counts the
 * suspends of setMode() unevenly (mSuspendCount 2 after the call), and every read() then only
 * sleeps; the microphone gave silence until the bridge restarted (measured 2026-11-02). Android
 * opens a new input stream for each recording, the bridge keeps its own: it opens it anew after
 * each call. Call with route_lock held. */
static void reopen_input(struct bridge *b)
{
	pthread_mutex_lock(&b->hal_in_lock);
	b->dev->close_input_stream(b->dev, b->in);
	b->in = open_input(b, atomic_load(&b->sco) ? AUDIO_DEVICE_IN_BLUETOOTH_SCO_HEADSET : AUDIO_DEVICE_IN_BUILTIN_MIC,
			   &b->in_channels);
	if (!b->in) {
		/* Without the stream the bridge cannot go on: the service restarts it */
		fprintf(stderr, "audio-hal-bridge: cannot reopen the HAL input after the call\n");
		abort();
	}
	pthread_mutex_unlock(&b->hal_in_lock);
}

static const char call_audio_xml[] =
	"<node>"
	" <interface name='org.mobian_project.CallAudio'>"
	"  <method name='SelectMode'><arg direction='in' name='mode' type='u'/>"
	"   <arg direction='out' name='success' type='b'/></method>"
	"  <method name='EnableSpeaker'><arg direction='in' name='enable' type='b'/>"
	"   <arg direction='out' name='success' type='b'/></method>"
	"  <method name='MuteMic'><arg direction='in' name='mute' type='b'/>"
	"   <arg direction='out' name='success' type='b'/></method>"
	"  <property name='AudioMode' type='u' access='read'/>"
	"  <property name='SpeakerState' type='u' access='read'/>"
	"  <property name='MicState' type='u' access='read'/>"
	" </interface>"
	"</node>";

static GVariant *call_audio_property(struct bridge *b, const char *name)
{
	if (!strcmp(name, "AudioMode"))
		return g_variant_new_uint32(b->call);
	if (!strcmp(name, "SpeakerState"))
		return g_variant_new_uint32(b->call ? b->call_speaker : !b->headphones);
	if (!strcmp(name, "MicState"))
		return g_variant_new_uint32(!b->mic_muted);
	return NULL;
}

static void call_audio_changed(struct bridge *b)
{
	GVariantBuilder changed;
	g_variant_builder_init(&changed, G_VARIANT_TYPE("a{sv}"));
	const char *names[] = {"AudioMode", "SpeakerState", "MicState"};
	for (size_t i = 0; i < G_N_ELEMENTS(names); i++)
		g_variant_builder_add(&changed, "{sv}", names[i], call_audio_property(b, names[i]));
	g_dbus_connection_emit_signal(b->bus, NULL, "/org/mobian_project/CallAudio", "org.freedesktop.DBus.Properties",
				      "PropertiesChanged",
				      g_variant_new("(sa{sv}as)", "org.mobian_project.CallAudio", &changed, NULL), NULL);
}

/* Call mode on or off. Call with route_lock held. */
static void select_mode(struct bridge *b, bool call)
{
	bool was_call = b->call;
	b->call = call;
	b->call_speaker = false;
	b->dev->set_mode(b->dev, b->call ? AUDIO_MODE_IN_CALL : AUDIO_MODE_NORMAL);
	if (was_call && !b->call)
		reopen_input(b);
	route(b);
	fprintf(stderr, "audio-hal-bridge: %s\n", b->call ? "call mode" : "normal mode");
	call_volume_schedule(b); /* the HAL gets it at every call's start, as on Android */
}

/* Reads the jingle: a RIFF/WAVE file, 16 bit PCM, two channels at RATE, at most CALL_END_MAX_S
 * long. Without it a call ends without a sound. Returns its frames. */
static size_t load_jingle(struct bridge *b, const char *path)
{
	gchar *data = NULL;
	gsize size = 0;
	if (!g_file_get_contents(path, &data, &size, NULL)) {
		fprintf(stderr, "audio-hal-bridge: no call end sound %s\n", path);
		return 0;
	}
	const uint8_t *p = (const uint8_t *)data;
	bool pcm = false;
	size_t frames = 0;
	for (size_t at = 12; size >= 12 && !memcmp(p, "RIFF", 4) && !memcmp(p + 8, "WAVE", 4) && at + 8 <= size;) {
		const uint8_t *c = p + at + 8;
		size_t len = p[at + 4] | p[at + 5] << 8 | p[at + 6] << 16 | (size_t)p[at + 7] << 24;
		if (len > size - at - 8)
			break;
		if (!memcmp(p + at, "fmt ", 4) && len >= 16) {
			unsigned tag = c[0] | c[1] << 8, channels = c[2] | c[3] << 8, bits = c[14] | c[15] << 8;
			unsigned long rate = c[4] | c[5] << 8 | c[6] << 16 | (unsigned long)c[7] << 24;
			pcm = tag == 1 && channels == 2 && bits == 16 && rate == RATE;
		} else if (!memcmp(p + at, "data", 4)) {
			if (pcm && len / 4 <= (size_t)CALL_END_MAX_S * RATE) {
				frames = len / 4;
				b->jingle = g_memdup2(c, frames * 4); /* little endian, as the HAL takes it */
			}
			break;
		}
		at += 8 + len + (len & 1);
	}
	g_free(data);
	if (!frames)
		fprintf(stderr, "audio-hal-bridge: call end sound %s is not 16 bit stereo PCM at %d Hz up to %d s\n", path,
			RATE, CALL_END_MAX_S);
	b->jingle_frames = frames;
	return frames;
}

/* feedbackd's profile "silent": a call ends without the jingle ("full" and "quiet" play it).
 * Without feedbackd's schema: not silent. */
static bool feedback_silent(void)
{
	GSettingsSchemaSource *src = g_settings_schema_source_get_default();
	GSettingsSchema *schema = src ? g_settings_schema_source_lookup(src, "org.sigxcpu.feedbackd", TRUE) : NULL;
	bool silent = false;
	if (schema && g_settings_schema_has_key(schema, "profile")) {
		GSettings *settings = g_settings_new_full(schema, NULL, NULL);
		char *profile = g_settings_get_string(settings, "profile");
		silent = !g_strcmp0(profile, "silent");
		g_free(profile);
		g_object_unref(settings);
	}
	if (schema)
		g_settings_schema_unref(schema);
	return silent;
}

/* Normal mode after the jingle, as SelectMode(0) without it. Main thread. */
static void call_end_finish(struct bridge *b, const char *why)
{
	pthread_mutex_lock(&b->route_lock);
	if (b->call_ending) {
		b->call_ending = false;
		if (b->call_end_timeout)
			g_source_remove(b->call_end_timeout);
		b->call_end_timeout = 0;
		pthread_mutex_lock(&b->out_lock);
		b->jingle_playing = false;
		pthread_mutex_unlock(&b->out_lock);
		fprintf(stderr, "audio-hal-bridge: call end sound %s\n", why);
		select_mode(b, false);
		if (b->bus)
			call_audio_changed(b);
	}
	pthread_mutex_unlock(&b->route_lock);
	bt_schedule(b); /* a headset leaves hands-free now */
}

static gboolean on_call_end_timeout(gpointer data)
{
	struct bridge *b = data;
	pthread_mutex_lock(&b->route_lock);
	b->call_end_timeout = 0;
	pthread_mutex_unlock(&b->route_lock);
	call_end_finish(b, "not through in time");
	return G_SOURCE_REMOVE;
}

/* The jingle's last transfer is with the HAL: normal mode once it has sounded */
static gboolean on_jingle_tail(gpointer data)
{
	struct jingle_played *played = data;
	struct bridge *b = played->bridge;
	pthread_mutex_lock(&b->route_lock);
	bool current = b->call_ending && played->gen == b->call_end_gen;
	pthread_mutex_unlock(&b->route_lock);
	if (current)
		call_end_finish(b, "played");
	g_free(played);
	return G_SOURCE_REMOVE;
}

static gboolean on_jingle_played(gpointer data)
{
	g_timeout_add(CALL_END_TAIL_MS, on_jingle_tail, data);
	return G_SOURCE_REMOVE;
}

/* A call ended: the jingle plays on the call's route (earpiece, speaker, or the headset over
 * SCO, whose hold stays while call mode does), call mode stays until it is through
 * (call_end_finish). As loud as the call; a headset sets its own volume. Call with route_lock
 * held, main thread. Returns false if no jingle plays: then normal mode follows at once. */
static bool call_end_start(struct bridge *b)
{
	if (!b->jingle_frames || feedback_silent())
		return false;
	float gain = 1.0f;
	if (!atomic_load(&b->sco)) {
		float slider = atomic_load(&b->role_phone_gain) >= 0.0f ? b->voice_applied : (float)b->call_step / CALL_STEPS;
		slider = SPA_CLAMP(slider, 1.0f / CALL_STEPS, 1.0f);
		gain = slider * slider * slider; /* as PipeWire turns a slider into a gain */
	}
	b->call_ending = true;
	b->call_end_gen++;
	pthread_mutex_lock(&b->out_lock);
	b->jingle_pos = 0;
	b->jingle_gain = gain;
	b->jingle_gen = b->call_end_gen;
	b->jingle_playing = true;
	pthread_cond_signal(&b->out_cond); /* out of standby */
	pthread_mutex_unlock(&b->out_lock);
	b->call_end_timeout = g_timeout_add(b->jingle_frames * 1000 / RATE + OUT_WARMUP_MS + CALL_END_SLACK_MS,
					    on_call_end_timeout, b);
	fprintf(stderr, "audio-hal-bridge: call end sound, gain %.2f\n", gain);
	return true;
}

/* A call again while the jingle plays: it stops, call mode stays. Call with route_lock held. */
static void call_end_cancel(struct bridge *b)
{
	b->call_ending = false;
	b->call_end_gen++; /* the jingle's end belongs to no call any more */
	if (b->call_end_timeout)
		g_source_remove(b->call_end_timeout);
	b->call_end_timeout = 0;
	pthread_mutex_lock(&b->out_lock);
	b->jingle_playing = false;
	pthread_mutex_unlock(&b->out_lock);
	fprintf(stderr, "audio-hal-bridge: call end sound cut short: a call again\n");
}

static void on_call_audio_method(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
				 const char *method, GVariant *params, GDBusMethodInvocation *invocation,
				 gpointer user_data)
{
	struct bridge *b = user_data;
	pthread_mutex_lock(&b->route_lock);
	if (!strcmp(method, "SelectMode")) {
		guint32 mode;
		g_variant_get(params, "(u)", &mode);
		if (mode > 1) {
			pthread_mutex_unlock(&b->route_lock);
			g_dbus_method_invocation_return_error(invocation, G_DBUS_ERROR, G_DBUS_ERROR_INVALID_ARGS,
							      "Unknown mode %u", mode);
			return;
		}
		if (mode == 1 && b->call_ending)
			call_end_cancel(b);
		/* After a call the jingle first, normal mode follows it (call_end_finish); the reply
		 * does not wait for it */
		if (!b->call_ending && !(mode == 0 && b->call && call_end_start(b)))
			select_mode(b, mode == 1);
	} else if (!strcmp(method, "EnableSpeaker")) {
		gboolean enable;
		g_variant_get(params, "(b)", &enable);
		b->call_speaker = enable;
		route(b);
	} else if (!strcmp(method, "MuteMic")) {
		gboolean mute;
		g_variant_get(params, "(b)", &mute);
		b->mic_muted = mute;
		b->dev->set_mic_mute(b->dev, mute);
	}
	call_audio_changed(b);
	pthread_mutex_unlock(&b->route_lock);
	bt_schedule(b); /* a call on a Bluetooth headset goes there */
	g_dbus_method_invocation_return_value(invocation, g_variant_new("(b)", TRUE));
}

static GVariant *on_call_audio_get(GDBusConnection *bus, const char *sender, const char *path, const char *iface,
				   const char *property, GError **error, gpointer user_data)
{
	struct bridge *b = user_data;
	pthread_mutex_lock(&b->route_lock);
	GVariant *value = call_audio_property(b, property);
	pthread_mutex_unlock(&b->route_lock);
	if (!value)
		g_set_error(error, G_DBUS_ERROR, G_DBUS_ERROR_UNKNOWN_PROPERTY, "Unknown property %s", property);
	return value;
}

static const GDBusInterfaceVTable call_audio_vtable = {on_call_audio_method, on_call_audio_get, NULL, {0}};

static void on_session_bus(GDBusConnection *bus, const char *name, gpointer user_data)
{
	struct bridge *b = user_data;
	GDBusNodeInfo *info = g_dbus_node_info_new_for_xml(call_audio_xml, NULL);
	b->bus = bus;
	g_dbus_connection_register_object(bus, "/org/mobian_project/CallAudio", info->interfaces[0],
					  &call_audio_vtable, b, NULL, NULL);
	g_dbus_node_info_unref(info);
}

static gboolean on_quit_signal(gpointer user_data)
{
	struct bridge *b = user_data;
	g_main_loop_quit(b->main_loop);
	return G_SOURCE_REMOVE;
}

int main(int argc, char *argv[])
{
	static struct bridge b = {
		.route_lock = PTHREAD_MUTEX_INITIALIZER,
		.ref_lock = PTHREAD_MUTEX_INITIALIZER,
		.routed_out = AUDIO_DEVICE_OUT_SPEAKER, /* as open_hal() opens the output */
		.speaker_routed = true,
		.out_lock = PTHREAD_MUTEX_INITIALIZER,
		.out_cond = PTHREAD_COND_INITIALIZER,
		.in_lock = PTHREAD_MUTEX_INITIALIZER,
		.in_cond = PTHREAD_COND_INITIALIZER,
		.hal_out_lock = PTHREAD_MUTEX_INITIALIZER,
		.hal_in_lock = PTHREAD_MUTEX_INITIALIZER,
		.fm_lock = PTHREAD_MUTEX_INITIALIZER,
		.fm_cond = PTHREAD_COND_INITIALIZER,
		.sco_restore = -1,
		.role_phone_gain = -1.0f,
	};

	describe_ports(&b);
	atomic_store(&b.volume_port, &b.speaker); /* until the "default" metadata says otherwise */
	load_call_volume(&b);
	load_jingle(&b, CALL_END_SOUND);
	if (open_hal(&b))
		return 1;
	b.aec = aec_new(RATE, 2, 1);
	if (!b.aec)
		fprintf(stderr, "audio-hal-bridge: no echo canceller\n");
	/* A hint only, AEC3 finds the delay itself: output latency plus one capture transfer */
	b.aec_delay = b.out->get_latency(b.out) + b.microphone.chunk / b.microphone.frame_size * 1000 / RATE;

	pw_init(&argc, &argv);
	b.loop = pw_thread_loop_new("audio-hal-bridge", NULL);
	pw_thread_loop_lock(b.loop);
	if (pw_thread_loop_start(b.loop) || connect_port(&b, &b.speaker) || connect_port(&b, &b.earpiece) ||
	    connect_port(&b, &b.microphone)) {
		pw_thread_loop_unlock(b.loop);
		fprintf(stderr, "audio-hal-bridge: cannot connect to PipeWire\n");
		return 1;
	}
	if (b.has_fm && connect_port(&b, &b.fm) < 0) {
		fprintf(stderr, "audio-hal-bridge: no FM source\n");
		b.has_fm = false;
	}
	watch_voice(&b);
	pw_thread_loop_unlock(b.loop);
	pthread_create(&b.out_thread, NULL, playback_thread, &b);
	pthread_create(&b.in_thread, NULL, capture_thread, &b);
	if (b.has_fm)
		pthread_create(&b.fm_thread, NULL, fm_thread, &b);
	b.jack_fd = open_jack();
	if (b.jack_fd >= 0)
		pthread_create(&b.jack_thread, NULL, jack_thread, &b);

	/* callaudiod's name: the device package points its D-Bus activation at this service */
	b.main_loop = g_main_loop_new(NULL, FALSE);
	guint owner = g_bus_own_name(G_BUS_TYPE_SESSION, "org.mobian_project.CallAudio", G_BUS_NAME_OWNER_FLAGS_NONE,
				     on_session_bus, NULL, NULL, &b, NULL);
	g_unix_signal_add(SIGINT, on_quit_signal, &b);
	g_unix_signal_add(SIGTERM, on_quit_signal, &b);
	g_main_loop_run(b.main_loop);
	g_bus_unown_name(owner);
	/* The headset back in its profile, the HAL off Bluetooth */
	pw_thread_loop_lock(b.loop);
	if (b.sco_state != SCO_OFF)
		sco_stop(&b, "stopping");
	pw_thread_loop_unlock(b.loop);

	pthread_mutex_lock(&b.out_lock);
	pthread_mutex_lock(&b.in_lock);
	pthread_mutex_lock(&b.fm_lock);
	b.quit = true;
	pthread_cond_signal(&b.out_cond);
	pthread_cond_signal(&b.in_cond);
	pthread_cond_signal(&b.fm_cond);
	pthread_mutex_unlock(&b.fm_lock);
	pthread_mutex_unlock(&b.in_lock);
	pthread_mutex_unlock(&b.out_lock);
	/* A thread blocked in the HAL returns within one transfer */
	pthread_join(b.out_thread, NULL);
	pthread_join(b.in_thread, NULL);
	if (b.has_fm)
		pthread_join(b.fm_thread, NULL);
	if (b.jack_fd >= 0) {
		/* Blocked in read() on the input device: cancel instead of waiting for an event */
		pthread_cancel(b.jack_thread);
		pthread_join(b.jack_thread, NULL);
		close(b.jack_fd);
	}

	pw_thread_loop_lock(b.loop);
	if (b.registry) {
		spa_hook_remove(&b.registry_listener);
		pw_proxy_destroy((struct pw_proxy *)b.registry);
	}
	if (b.core)
		pw_core_disconnect(b.core);
	if (b.context)
		pw_context_destroy(b.context);
	pw_stream_destroy(b.speaker.stream);
	pw_stream_destroy(b.earpiece.stream);
	if (b.phones.stream)
		pw_stream_destroy(b.phones.stream);
	pw_stream_destroy(b.microphone.stream);
	if (b.bt_mic.stream)
		pw_stream_destroy(b.bt_mic.stream);
	if (b.fm.stream)
		pw_stream_destroy(b.fm.stream);
	pw_thread_loop_unlock(b.loop);
	pw_thread_loop_stop(b.loop);
	pw_thread_loop_destroy(b.loop);

	b.dev->close_output_stream(b.dev, b.out);
	b.dev->close_input_stream(b.dev, b.in);
	b.dev->common.close(&b.dev->common);
	if (b.aec)
		aec_free(b.aec);
	g_free(b.jingle);
	pw_deinit();
	return 0;
}
