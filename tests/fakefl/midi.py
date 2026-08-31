PEAK_L, PEAK_R, PEAK_LR, PEAK_LR_INV = 0, 1, 2, 3
TN_Master, TN_FirstIns, TN_LastIns, TN_Sel = 0, 1, 2, 3
PIM_None = -1
GC_BackgroundColor = 0
FPN_Param = 0
FPN_Semitone = 2
FPN_Preset = 6
GPN_GetCurrentPreset = -1
PAD_Count, PAD_Semitone, PAD_Color, PAD_Empty, PAD_Muted = 0, 1, 2, 3, 4
UF_None, UF_EE, UF_PR, UF_PL = 0, 1, 2, 4
UF_Knob, UF_AudioRec, UF_AutoClip = 32, 256, 512
UF_PRMarker, UF_PLMarker, UF_Plugin = 1024, 2048, 4096
SONGLENGTH_MS, SONGLENGTH_S, SONGLENGTH_ABSTICKS = 0, 1, 2
SONGLENGTH_BARS, SONGLENGTH_STEPS, SONGLENGTH_TICKS = 3, 4, 5
GT_All = 15
GT_Global = 8
PME_System = 2
FPT_Metronome = 110
FPT_CountDown = 115
widPianoRoll = 3
FromMIDI_Max = 65536
REC_Chan_Vol = 0
REC_Chan_Pan = 1
REC_Mixer_Vol = 536879040
REC_Mixer_Pan = 536879041
REC_Mixer_SS = 536879042
REC_UpdateValue = 1
REC_ShowHint = 4
REC_UpdateControl = 32
REC_FromMIDI = 64
REC_Store = 128
REC_Init = 256
REC_SetChanged = 512
REC_SetTouched = 1024
REC_Control = (
    REC_UpdateValue
    | REC_UpdateControl
    | REC_ShowHint
    | REC_Store
    | REC_Init
    | REC_SetChanged
    | REC_SetTouched
)
REC_MIDIController = REC_Control | REC_FromMIDI
