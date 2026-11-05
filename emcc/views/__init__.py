"""Device views and the host that routes to them.

A *view* renders the device set. Three exist: the list (`list_view.ListView`),
the dashboard (`views/dashboard_view.DashboardView`) and the device-log
settings (`settings_view.SettingsView`). They are deliberately different
shapes -- the Dashboard *is* a `CTkFrame`, `ListView` and `SettingsView` are
controllers that own one -- which is why `base.DeviceView` declares `widget`
rather than assuming a view is one.

`host.ViewHost` owns registration, switching, render routing and exception
containment. It contributes no widget of its own: it packs `view.widget`
straight into the parent it is handed, so no frame is inserted between `App`
and the views and no device-card pathname moves.

See `VIEW_CONTRACT.md` for the call order, what `ctx` owes a view, and the
two hard requirements on the host.
"""
