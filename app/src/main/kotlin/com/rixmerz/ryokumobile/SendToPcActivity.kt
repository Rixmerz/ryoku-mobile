package com.rixmerz.ryokumobile

import android.app.Activity
import android.content.Intent
import android.os.Bundle

/**
 * "Send to PC" in a share sheet. YouTube's Share -> Send to PC lands here with the video's link; the
 * activity has no screen of its own and only passes the text on. It is the manual way to the same
 * handoff the button in [CastService] automates.
 */
class SendToPcActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        intent.getStringExtra(Intent.EXTRA_TEXT)?.let { PcHandoff.deliverShared(applicationContext, it) }
        finish()
    }
}
