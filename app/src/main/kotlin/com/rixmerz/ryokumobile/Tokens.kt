package com.rixmerz.ryokumobile

import androidx.compose.runtime.Composable
import androidx.compose.runtime.ReadOnlyComposable
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.lerp
import androidx.compose.ui.text.font.Font
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontVariation
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * The phone's copy of Ryoku's Tokens.qml (docs/ui-ux.md): every surface reads colour, type and
 * geometry from here and nothing hardcodes a value.
 */
data class Scheme(val id: String, val label: String, val dark: Boolean, val swatches: List<Long>)

/** Signature default: pure-black paper, warm bone ink, vermillion sun. Not part of the catalog. */
val signature = Scheme("Ryoku", "Ryoku", true,
    listOf(0xFF000000, 0xFFCDC4BA, 0xFFE2342A, 0xFFB0A9A0, 0xFF958F87, 0xFFE2342A, 0xFF7A756E))

val schemes: List<Scheme> = listOf(signature) + catalogSchemes

fun schemeById(id: String) = schemes.firstOrNull { it.id == id } ?: signature

/** Resolved roles. Signature uses the doc's exact four-tier ramp; catalog themes derive it. */
class Tokens(s: Scheme) {
    private val c = s.swatches.map { Color(it) }
    val paper = c[0]
    val ink = c[1]
    val inkDim = if (s === signature) Color(0xFFB0A9A0) else lerp(c[1], c[0], 0.16f)
    val inkMuted = if (s === signature) Color(0xFF958F87) else c[6]
    val inkFaint = if (s === signature) Color(0xFF7A756E) else lerp(c[6], c[0], 0.22f)
    val paperLift = lerp(c[0], c[1], 0.04f)
    val bone = ink            // inverse surface: emphasis is inversion, not colour
    val inkOnBone = paper
    val sun = c[2]            // the accent: frame and art only, never content
    val secondary = c[3]
    val tertiary = c[4]
    val line = ink.copy(alpha = 0.26f)
    val lineSoft = ink.copy(alpha = 0.13f)
    val lineStrong = ink.copy(alpha = 0.42f)
    val tint10 = ink.copy(alpha = 0.10f)

    companion object {
        val brand = Color(0xFFE2342A) // the 力 seal: never themes, that is the point
        val alert = Color(0xFFE2342A)
    }
}

val LocalTokens = staticCompositionLocalOf { Tokens(signature) }

val T: Tokens @Composable @ReadOnlyComposable get() = LocalTokens.current

object Type {
    val ui = FontFamily(
        Font(R.font.grotesk_light, FontWeight.Light),
        Font(R.font.grotesk_regular, FontWeight.Normal),
        Font(R.font.grotesk_medium, FontWeight.Medium),
    )
    @OptIn(androidx.compose.ui.text.ExperimentalTextApi::class)
    val display = FontFamily(
        Font(R.font.fraunces, FontWeight.Normal, variationSettings = FontVariation.Settings(FontVariation.weight(400), FontVariation.Setting("opsz", 144f), FontVariation.Setting("WONK", 0f))),
        Font(R.font.fraunces, FontWeight.Medium, variationSettings = FontVariation.Settings(FontVariation.weight(500), FontVariation.Setting("opsz", 144f), FontVariation.Setting("WONK", 0f))),
        Font(R.font.fraunces, FontWeight.Light, variationSettings = FontVariation.Settings(FontVariation.weight(300), FontVariation.Setting("opsz", 144f), FontVariation.Setting("WONK", 0f))),
    )
    val mono = FontFamily(Font(R.font.mono_regular))
    // Kanji seals fall back to the system Noto Sans CJK, which every Android build ships.
    val jp = FontFamily.Default

    // The eight-step ramp: a step is a role, not a number to pick.
    val fTitle = 32.sp; val fHero = 34.sp; val fValue = 26.sp; val fRow = 15.sp
    val fBody = 14.sp; val fSmall = 13.sp; val fMicro = 11.sp; val fTiny = 9.sp
    val trackLabel = 1.4.sp; val trackMark = 2.2.sp
}

object Geo {
    val radius = 6.dp
    val border = 1.dp
    val s1 = 4.dp; val s2 = 8.dp; val s3 = 12.dp; val s4 = 16.dp; val s5 = 24.dp; val s6 = 32.dp; val s7 = 48.dp
    val rowH = 48.dp
    val ctlH = 26.dp
}
