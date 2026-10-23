"""Explicit initial output choices, independent of installed FFmpeg capabilities."""


PCM_CODECS = {"pcm_s16le": 16, "pcm_s24le": 24}
MP3_SAMPLE_RATES = frozenset((8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000))


def validate_settings(settings) -> None:
    """Reject inconsistent settings without choosing or correcting any target."""
    if type(settings.sample_rate) is not int or not 0 < settings.sample_rate <= 2**31 - 1:
        raise ValueError("sample_rate must be a positive 32-bit integer in Hz")
    if settings.channel_policy != "preserve":
        raise ValueError("only the preserve channel policy is supported; no downmixing is planned")
    if settings.container == "wav":
        if settings.codec not in PCM_CODECS:
            raise ValueError("WAV targets support pcm_s16le and pcm_s24le only")
        if type(settings.bit_depth) is not int or settings.bit_depth != PCM_CODECS[settings.codec]:
            raise ValueError("PCM bit depth must explicitly match the selected codec")
        if settings.quality is not None:
            raise ValueError("PCM targets do not accept lossy encoding quality")
    elif settings.container == "mp3":
        if settings.codec != "mp3":
            raise ValueError("MP3 targets require codec mp3 (encoded by libmp3lame)")
        if settings.bit_depth is not None:
            raise ValueError("MP3 targets do not accept a PCM bit depth")
        if type(settings.quality) is not int or not 0 <= settings.quality <= 9:
            raise ValueError("MP3 VBR quality must be an integer from 0 (highest) to 9")
        if settings.sample_rate not in MP3_SAMPLE_RATES:
            raise ValueError("the target sample rate is unsupported by MP3")
    else:
        raise ValueError("supported output containers are wav and mp3")
