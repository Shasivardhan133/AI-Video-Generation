"""Model profiles: the only generation settings the application accepts.

A profile is an application-owned name for one Wan 2.1 configuration. Users can
only choose values listed here, so unsupported or unsafe model parameters never
reach the GPU service.

`t4_verified` stays False until the profile has actually been run on the Colab
T4 (roadmap phase 2). The values below come from the official Wan 2.1 release:
the 1.3B text-to-video model supports 832x480 and 480x832, and the default clip
is 81 frames at 16 fps (about 5 seconds). Frame counts must have the form 4n + 1.
"""

from dataclasses import dataclass, field

TEXT_TO_VIDEO = 'text_to_video'
IMAGE_TO_VIDEO = 'image_to_video'

STYLES = ('cinematic', 'realistic', 'anime', '3d_animation')


@dataclass(frozen=True)
class ModelProfile:
    name: str
    mode: str
    wan_task: str
    sizes: tuple
    min_frames: int = 17
    max_frames: int = 81
    default_frames: int = 81
    fps: int = 16
    supports_prompt_enhancement: bool = True
    t4_verified: bool = False
    notes: str = ''
    sample_options: dict = field(default_factory=dict)

    def default_size(self):
        return self.sizes[0]

    def allowed_frame_counts(self):
        return [n for n in range(self.min_frames, self.max_frames + 1) if (n - 1) % 4 == 0]


PROFILES = {
    'wan2.1-t2v-1.3b-480p': ModelProfile(
        name='wan2.1-t2v-1.3b-480p',
        mode=TEXT_TO_VIDEO,
        wan_task='t2v-1.3B',
        sizes=((832, 480), (480, 832)),
        notes='Official 1.3B model; needs about 8 GB VRAM with model offloading.',
        sample_options={'sample_shift': 8, 'sample_guide_scale': 6},
    ),
    'wan2.1-i2v-14b-480p': ModelProfile(
        name='wan2.1-i2v-14b-480p',
        mode=IMAGE_TO_VIDEO,
        wan_task='i2v-14B',
        sizes=((832, 480), (480, 832)),
        supports_prompt_enhancement=False,
        notes='Wan 2.1 image-to-video only ships as a 14B model; T4 feasibility is unproven.',
    ),
}

DEFAULT_PROFILE = {
    TEXT_TO_VIDEO: 'wan2.1-t2v-1.3b-480p',
    IMAGE_TO_VIDEO: 'wan2.1-i2v-14b-480p',
}


def get_profile(name):
    return PROFILES.get(name)


def profiles_for_mode(mode):
    return [profile for profile in PROFILES.values() if profile.mode == mode]
