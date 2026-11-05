package com.rixmerz.ryokumobile

import android.service.notification.NotificationListenerService

/**
 * Notification access, granted once through Shizuku. Holding it is what lets the launcher read
 * active media sessions and notification counts; the service itself needs no callbacks for that.
 */
class MediaListener : NotificationListenerService()
