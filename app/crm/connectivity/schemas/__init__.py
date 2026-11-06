"""Leaf shapes, one module per table family. Exports nothing — import the
family you mean by its full path, so a reader of any logic file can see which
family a shape belongs to without opening a second file.

Nothing internal is imported here beyond a sibling in this package: a shape
may compose another shape (message.SendRoute holds connector.ConnectorInstallation),
but no schemas module imports a logic, db or provider module. That is what
keeps this the layer everything else may depend on.
"""
