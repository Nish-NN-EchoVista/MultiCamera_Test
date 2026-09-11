"""Device views and the host that routes to them.

A *view* renders the device set. Two exist: the list (`list_view.ListView`)
and the dashboard (`views/dashboard_view.DashboardView`). They are
deliberately different shapes -- the Dashboard *is* a `CTkFrame`, `ListView`
is a controller that owns one -- which is why `base.DeviceView` declares
`widget` rather than assuming a view is one.

`host.ViewHost` owns registration, switching, render routing and exception
containment. It contributes no widget of its own: it packs `view.widget`
straight into the parent it is handed, so no frame is inserted between `App`
and the views and no device-card pathname moves.

See `VIEW_CONTRACT.md` for the call order, what `ctx` owes a view, and the
two hard requirements on the host.
"""
