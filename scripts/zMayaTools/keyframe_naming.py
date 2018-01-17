from pymel import core as pm
import maya.OpenMaya as om
import maya.OpenMayaAnim as oma
from maya.app.general import mayaMixin
from maya.app.general.mayaMixin import MayaQWidgetDockableMixin
from zMayaTools import qt_helpers, maya_logging, maya_helpers, Qt
import bisect, os, sys, time
from collections import defaultdict
from pprint import pprint, pformat

log = maya_logging.get_log()

plugin_node_id = om.MTypeId(0x12474B)

def get_singleton(create=True):
    """
    Return the singleton node.  If create is true, create it if it doesn't exist,
    otherwise return None.
    """
    nodes = pm.ls(':zKeyframeNaming', type='zKeyframeNaming')
    if not nodes:
        if not create:
            return None
        return pm.createNode('zKeyframeNaming', name=':zKeyframeNaming')
    assert len(nodes) == 1
    return nodes[0]

def _get_key_index_at_frame(frame):
    """
    Get the key index for the current frame.

    If there's no key at the current frame, return None rather than the value of
    the most recent frame.
    """
    keys = get_singleton(create=False)
    if keys is None:
        return None

    idx = pm.keyframe(keys.attr('keyframes'), q=True, valueChange=True, t=frame)
    if idx:
        return int(idx[0])
    else:
        return None

def key_exists_at_frame(frame):
    """
    Return true if a key is set at the given frame.
    """
    return _get_key_index_at_frame(frame) is not None

def find_frame_of_key(frame):
    """
    Return the nearest key on or before frame, or None if there aren't any.
    """
    keys = get_singleton(create=False)
    if keys is None:
        return None
    
    # Why is there no <= search?
    frames = pm.findKeyframe(keys.attr('keyframes'), t=frame + 0.000001, which='previous')
    if frames is None:
        return None
    if frames > frame:
        return None
    return frames

   
def get_all_keys():
    """
    Return the time and name index of all named keys.
    """
    keys = get_singleton(create=False)
    if keys is None:
        return {}

    time_and_index = pm.keyframe(keys.attr('keyframes'), q=True, valueChange=True, timeChange=True, absolute=True)

    # keyframe() returns floats, even though the index values are integers.  Convert them
    # to ints.
    time_and_index = {frame: int(idx) for frame, idx in time_and_index}
    return time_and_index

def get_all_names():
    """
    Return a dictionary of all names, indexed by index.
    """
    keys = get_singleton(create=False)
    if keys is None:
        return {}
    
    entries = keys.attr('entries')

    result = {}
    for entry in entries:
        idx = entry.index()
        result[idx] = entry.attr('name').get()
    return result

def get_name_at_idx(idx):
    """
    Return a single name.
    """
    if idx is None:
        return None

    keys = get_singleton(create=False)
    if keys is None:
        return None
    return keys.attr('entries').elementByLogicalIndex(idx).attr('name').get()

def get_name_at_frame(frame):
    idx = _get_key_index_at_frame(frame)
    if idx is None:
        return None

    return get_name_at_idx(idx)

def set_name_at_frame(frame, name):
    keys = get_singleton(False)
    if keys is None:
        return

    # Run index cleanup if needed before making changes to the frame.
    cleanup_duplicate_indices()    

    idx = _get_key_index_at_frame(frame)
    if idx is None:
        return

    attr = keys.attr('entries').elementByLogicalIndex(idx).attr('name')

    # Don't set the name if it isn't changing, so an undo entry isn't created.
    if attr.get() != name:
        attr.set(name)

def _get_unused_name_index():
    """
    Return the first unused index in the name list.
    """
    # Get the full key list, so we can find an unused slot.
    #
    # Note that we're looking at the entries actually referenced from keyframes and
    # not just calling get(mi=True) on entries, so we'll reuse stale entries.
    name_indices = get_all_keys().values()
    name_indices.sort()

    # Search for the first unused index.
    prev_idx = -1
    for idx in name_indices:
        if idx != prev_idx + 1:
            break
        prev_idx = idx
    return prev_idx + 1

   
def create_key_at_time(frame):
    """
    Create a key at the given time.  If a key already exists, return its index.
    """
    keys = get_singleton()
    
    # Find the name index for frame, if it already exists.
    idx = _get_key_index_at_frame(frame)
    if idx is not None:
        return idx

    # There's no key at the current frame.  Find an unused name index and create it.
    # We have to set the value, then set the keyframe.  If we just call setKeyframe,
    # the value won't be set correctly if it's in a character set.
    idx = _get_unused_name_index()
    keys.attr('keyframes').set(idx)

    # Disable auto-keyframe while we do this.  Otherwise, a keyframe will also
    # be added at the current frame (which seems like a bug).
    with maya_helpers.disable_auto_keyframe():
        pm.setKeyframe(keys, at='keyframes', time=frame, value=idx)

    # setKeyframe can do this, but it's buggy: outTangentType='step' isn't applied if
    # we add a key before any other existing keys.
    pm.keyTangent(keys, time=frame, inTangentType='stepnext', outTangentType='step')

    # Keyframes can be deleted by the user, which leaves behind stale entries.  Remove
    # any leftover data in the slot we're using.
    pm.removeMultiInstance(keys.attr('entries').elementByLogicalIndex(idx))

    return idx

def delete_key_at_frame(frame):
    """
    Delete the key at frame.

    Note that we don't delete the underlying zKeyframeNaming node if it's empty, since it might
    be added to character sets by the user.
    """
    keys = get_singleton()

    # Run index cleanup if needed before making changes to the frame.
    cleanup_duplicate_indices()    

    all_keys = get_all_keys()

    idx = pm.keyframe(keys.attr('keyframes'), q=True, valueChange=True, t=frame)
    if not idx:
        return

    # Remove the keyframe and any associated data.
    pm.cutKey(keys.attr('keyframes'), t=frame)
    pm.removeMultiInstance(keys.attr('entries').elementByLogicalIndex(idx[0]))

def cleanup_duplicate_indices():
    """
    Clean up duplicate entries in the key index.

    If the user copies and pastes keyframe indices in the graph editor, we'll
    end up with multiple frames pointing at the same name entry.  If we edit
    those entries without cleaning it up first, we'll cause unwanted changes.
    """
    keys = get_singleton()
    
    all_keys = get_all_keys()
    keys_by_index = defaultdict(list)
    for frame, index in all_keys.items():
        keys_by_index[index].append(frame)

    for idx, frames in keys_by_index.items():
        if len(frames) < 2:
            continue
            
        # Sort frames, so we always leave the first one alone and adjust the rest.
        frames.sort()

        name = get_name_at_idx(idx)
        for frame in frames[1:]:
            # Delete the duplicate index and create a new one with the same name.
            pm.cutKey(keys.attr('keyframes'), t=frame)
            create_key_at_time(frame)
            set_name_at_frame(frame, name)
           
    return True

def connect_to_arnold():
    """
    If mtoa is loaded, attach the current frame name to a custom EXR attribute,
    so the frame name is exported with renders.
    """
    if not pm.pluginInfo('mtoa', q=True, loaded=True):
        log.warning('The Arnold plugin isn\'t loaded.')
        return

    # Find the Arnold driver node, if it exists.
    driver = pm.ls('defaultArnoldDriver')
    if not driver:
        log.warning('The Arnold driver doesn\'t exist.  Select Arnold as the scene renderer first.')
        return

    driver = driver[0]

    # Get the output attribute.
    keys = get_singleton()
    output = keys.attr('arnoldAttributeOut')

    # Get Arnold's array of custom attributes.
    attrs = driver.attr('customAttributes')

    # See if the output attribute is already connected to a custom attribute, so we
    # don't create it more than once.
    for conn in output.listConnections(s=False, d=True, p=True):
        if not conn.isElement():
            continue
        if conn.array() == attrs:
            log.info('An Arnold attribute has already been created.')
            return
    
    # Connect it to the next unused EXR attribute.
    idx = pm.mel.eval('getNextFreeMultiIndex %s 0' % attrs)
    input = attrs.elementByLogicalIndex(idx)
    output.connect(input)
    log.info('Arnold attribute created.')

class KeyframeNamingWindow(MayaQWidgetDockableMixin, Qt.QDialog):
    def __init__(self):
        super(KeyframeNamingWindow, self).__init__()

        # How do we make our window handle global hotkeys?
#        undo = Qt.QAction('Undo', self)
#        undo.setShortcut(Qt.Qt.CTRL + Qt.Qt.Key_Z)
#        undo.triggered.connect(lambda: pm.undo())
#        self.addAction(undo)

#        redo = Qt.QAction('Redo', self)
#        redo.setShortcut(Qt.Qt.CTRL + Qt.Qt.Key_Y)
#        redo.triggered.connect(lambda: pm.redo(redo=True))
#        self.addAction(redo)

        self.shown = False
        self.callback_ids = om.MCallbackIdArray()

        self.frames_in_list = []
        self._currently_refreshing = False
        self._currently_setting_selection = False
        self._listening_to_anim_curve = None

        self.time_change_listener = maya_helpers.TimeChangeListener(self._time_changed)

        # Make sure zKeyframeNaming has been generated.
        qt_helpers.compile_all_layouts()

        from qt_generated import keyframe_naming
        reload(keyframe_naming)

        self.ui = keyframe_naming.Ui_keyframe_naming()
        self.ui.setupUi(self)

        self.ui.removeFrame.clicked.connect(self.delete_selected_frame)
        self.ui.renameFrame.clicked.connect(self.rename_selected_frame)
        self.ui.addFrame.clicked.connect(self.add_new_frame)
        self.ui.frameList.itemDelegate().commitData.connect(self.frame_name_edited)
        self.ui.frameList.itemDelegate().closeEditor.connect(self.name_editor_closed)
        self.ui.frameList.itemSelectionChanged.connect(self.selected_frame_changed)
        self.ui.frameList.itemClicked.connect(self.selected_frame_changed)

        # Create the menu.  Why can't this be done in the designer?
        menu_bar = Qt.QMenuBar()
        self.layout().setMenuBar(menu_bar)

        edit_menu = menu_bar.addMenu('Edit')
        add_arnold_attribute = Qt.QAction('Add Arnold attribute', self)
        add_arnold_attribute.setStatusTip('Add a custom Arnold attribute to export the current frame name to rendered EXR files')
        add_arnold_attribute.triggered.connect(connect_to_arnold)
        edit_menu.addAction(add_arnold_attribute)

        self.installEventFilter(self)
        self.ui.frameList.installEventFilter(self)

        # showEvent() will be called when we're actually displayed, and fill in the list.

    def eventFilter(self, object, event):
        if object is self:
            if event.type() == Qt.QEvent.KeyPress:
                if event.key() == Qt.Qt.Key_Delete:
                    self.delete_selected_frame()
                    return True
                elif event.key() == Qt.Qt.Key_Insert:
                    self.add_new_frame()
                    return True
        elif object is self.ui.frameList:
            if event.type() == Qt.QEvent.KeyPress:
                if event.key() == Qt.Qt.Key_Return:
                    self.rename_selected_frame()

        return super(KeyframeNamingWindow, self).eventFilter(object, event)

    def done(self, result):
        """
        This is called when the window is closed.
        """
        self.close()
        super(MayaQWidgetDockableMixin, self).done(result)

    def get_selected_frame_item(self):
        """
        Return the QListWidgetItem for the frame selected in the list, or None if
        nothing is selected.
        """
        selection = self.ui.frameList.selectedItems()
        if not selection:
            return None

        return selection[0]

    def selected_frame_changed(self):
        """
        Set the scene time to the selected frame.
        """
        # If self.set_selected_frame is setting the selection, don't change the scene time.
        if self._currently_setting_selection:
            return

        selection = self.get_selected_frame_item()
        if not selection:
            return

        pm.currentTime(selection.frame)
       
    def set_selected_frame(self, frame):
        """
        Set the selected frame in the list.
        """
        # Binary search for the nearest frame on or before frame.
        idx = max(bisect.bisect_right(self.frames_in_list, frame) - 1, 0)
        if idx >= self.ui.frameList.count():
            return

        item = self.ui.frameList.item(idx)

        # Let selected_frame_changed know that we're setting the selection explicitly, so
        # it shouldn't sync the scene time up with it.
        self._currently_setting_selection = True
        try:
            self.ui.frameList.setCurrentItem(item)
        finally:
            self._currently_setting_selection = False

    def set_selected_frame_from_current_time(self):
        """
        Select the frame in the list from the current time.
        """
        self.set_selected_frame(pm.currentTime(q=True))

    def cancel_rename(self):
        """
        If an entry is being renamed, cancel it.
        """
        if self.ui.frameList.state() != Qt.QAbstractItemView.EditingState:
            return

        item = self.get_selected_frame_item()
        self.ui.frameList.closePersistentEditor(item)

    def add_new_frame(self):
        """
        Create a key if one doesn't exist already, and begin editing its name.
        """
        with maya_helpers.undo('Create named keyframe'):
            # If we're editing, cancel editing before adding the new frame, or the
            # new frame won't be visible.  This can happen if you select a frame,
            # click Add, then select another frame and click Add without first
            # pressing enter for the first rename.  This usually only happens if
            # the window is docked into the main Maya window.
            self.cancel_rename()

            if not key_exists_at_frame(pm.currentTime(q=True)):
                frame = pm.currentTime(q=True)
                create_key_at_time(frame)
                set_name_at_frame(frame, 'Frame %i' % frame)
                
            # Our listeners will refresh automatically, but that won't happen until later.  Refresh
            # immediately, so we can initiate editing on the new item.
            self.refresh()

            # Find the new item and edit it to let the user set its name.
            self.rename_selected_frame()

    def delete_selected_frame(self):
        """
        Delete the frame that's selected in the list, if any.
        """
        item = self.get_selected_frame_item()
        if item is None:
            return

        self.cancel_rename()
        with maya_helpers.undo('Delete keyframe bookmark'):
            delete_key_at_frame(item.frame)
    
    def rename_selected_frame(self):
        """
        Rename the frame selected in the list, if any.
        """
        selection = self.get_selected_frame_item()
        if not selection:
            return

        self.ui.frameList.editItem(selection)

    def refresh(self):
        if not self.shown:
            return

        # Don't refresh while editing.
        if self.ui.frameList.state() == Qt.QAbstractItemView.EditingState:
            return

        self._currently_refreshing = True
        try:
            all_keys = get_all_keys()
            all_names = get_all_names()
            self.ui.frameList.clear()
            self.frames_in_list = []

            # Add keys in chronological order.
            for frame in sorted(all_keys.keys()):
                idx = all_keys[frame]
                name = all_names.get(idx, '')
                item = Qt.QListWidgetItem(name)
                item.frame = frame
                item.setFlags(item.flags() | Qt.Qt.ItemIsEditable)

                self.ui.frameList.addItem(item)

                self.frames_in_list.append(frame)

            self.set_selected_frame_from_current_time()
        finally:
            self._currently_refreshing = False

    def _time_changed(self):
        """
        When the scene time changes, update the current selection to match.

        This isn't called during playback.
        """
        qt_helpers.run_async_once(self.set_selected_frame_from_current_time)

    def _register_listeners(self):
        if not self.shown:
            return

        # Stop if we've already registered listeners.
        if self.callback_ids.length():
            return

        msg = om.MDGMessage()
        self.callback_ids.append(msg.addNodeAddedCallback(self._keyframe_naming_nodes_changed, 'zKeyframeNaming', None))
        self.callback_ids.append(msg.addNodeRemovedCallback(self._keyframe_naming_nodes_changed, 'zKeyframeNaming', None))
        self.callback_ids.append(msg.addConnectionCallback(self._connection_changed, None))
        node = get_singleton(create=False)

        if node is not None:
            self.callback_ids.append(om.MNodeMessage.addNameChangedCallback(node.__apimobject__(), self._node_renamed))
            self.callback_ids.append(om.MNodeMessage.addAttributeChangedCallback(node.__apimobject__(), self._keyframe_node_changed, None))
            anim_curve = self._get_keyframe_anim_curve()
            self._listening_to_anim_curve = anim_curve

            # If the keyframes attribute is animated, listen for keyframe changes.
            if anim_curve is not None:
                self.callback_ids.append(oma.MAnimMessage.addNodeAnimKeyframeEditedCallback(anim_curve, self._keyframe_keys_changed))

        self.time_change_listener.register()

    def _unregister_listeners(self):
        if self.callback_ids:
            # Why is the unregistering API completely different from the registering API?
            msg = om.MMessage()
            msg.removeCallbacks(self.callback_ids)
            self.callback_ids.clear()

        self.time_change_listener.unregister()
        self._listening_to_anim_curve = None

    def _keyframe_keys_changed(self, *args):
        self._async_refresh()

    def _keyframe_naming_nodes_changed(self, node, data):
        # A zKeyframeNaming node was added or removed, so refresh the list.  Queue this instead of doing
        # it now, since node removed callbacks happen before the node is actually deleted.
        qt_helpers.run_async_once(self.refresh)

    def _node_renamed(self, node, old_name, unused):
        # A node was renamed.  Refresh the node list if it's a zKeyframeNaming node.
        dep_node = om.MFnDependencyNode(node)
        if dep_node.typeId() != plugin_node_id:
            return
        
        qt_helpers.run_async_once(self.refresh)

    @classmethod
    def _get_keyframe_anim_curve(cls):
        """
        Return the animCurve node controlling keyframes, or None if keyframes
        isn't animated.

        A raw MObject is returned.
        """
        keys = get_singleton()

        result = om.MObjectArray()
        oma.MAnimUtil.findAnimation(keys.attr('keyframes').__apimplug__(), result)
        if result.length() == 0:
            return None
        else:
            #return pm.PyNode(result[0])
            return result[0]

    def _check_file_loading(self):
        """
        Unregister our scene callbacks during file loads.  We don't want to slow file operations
        by having callbacks run while loading large scenes, and for some reason registering callbacks
        during file I/O can cause problems with array data not being set.

        However, there's no callback for when isOpeningFile or isReadingFile change value, and
        MSceneMessage is a pain for dealing with reference loads.

        Handle this by just unregistering our callbacks the first time we're called during a
        file load, and reregistering them in the idle loop, which will happen after the file
        load finishes.  Return true if we're currently in a file load and the current callback
        should stop.
        """
        # isOpeningFile is true while opening a file, but not while loading a reference.
        # isReadingFile is true while loading references, but not while loading files.
        if not om.MFileIO.isOpeningFile() and not om.MFileIO.isReadingFile():
            return False

        def reestablish_callbacks():
            self._register_listeners()
            self.refresh()

        self._unregister_listeners()

        # Reregister our listeners once the file operation finishes.
        qt_helpers.run_async_once(reestablish_callbacks)
        
        return True

    def _connection_changed(self, src_plug, dst_plug, made, data):
        if self._check_file_loading():
            return

        # If a keyframe node is connected or disconnected from zKeyframeNaming.keyframes,
        # we need to reestablish listeners to listen for keyframe changes.
        #
        # There's no obvious quick way to find out if this connection affects that, though.
        # We can't just look at the plugs, since there might be other nodes in between, like
        # character sets.  Instead, we have to look at the actual animation curve node and
        # see if it's changed.
        anim_curve = self._get_keyframe_anim_curve()
        if anim_curve is self._listening_to_anim_curve:
            # The animation curve we're interested hasn't changed.
            return

        if not self.callback_ids.length():
            # Our listeners are unregistered anyway, so don't register them.
            return

        # Reset listeners and refresh the display.
        self._unregister_listeners()
        self._register_listeners()
        qt_helpers.run_async_once(self.refresh)
        
    def _keyframe_node_changed(self, msg, plug, otherPlug, data):
        # For some reason, this is called once per output, but not actually called for changed inputs.
        # It seems to not notice when a value has changed because its input key connection has changed.
        #
        # kAttributeSet is sent for most things, like moving the time slider causing the current key
        # to change and us making changes directly.  kAttributeEval catches some things that doesn't,
        # in particular editing keys with the graph editor, but this only works if something is connected
        # to the output to trigger an evaluation.  Note that Set usually comes from the main thread, but
        # Eval tends to come from a worker thread, so we depend on the async dispatching to move this to
        # the main thread.
        if msg & (om.MNodeMessage.kAttributeSet|om.MNodeMessage.kAttributeEval):
            self._async_refresh()

    def _async_refresh(self):
        """
        Queue a refresh.  If this is called multiple times before we do the refresh, we'll only
        refresh once.
        """
        qt_helpers.run_async_once(self.refresh)

    def frame_name_edited(self, widget):
        # How do you find out which item was edited?  QT's documentation is useless.
        items = self.ui.frameList.selectedItems()
        if not items:
            return
        item = items[0]

        with maya_helpers.undo('Rename keyframe bookmark'):
            set_name_at_frame(item.frame, item.text())

    def name_editor_closed(self, editor, hint):
        # We don't refresh while editing, so we don't clobber the user's edits.  Refresh
        # after editing finishes.
        self.refresh()

    def __del__(self):
        self.cleanup()

    def cleanup(self):
        self._unregister_listeners()

    def showEvent(self, event):
        # Why is there no isShown()?
        if self.shown:
            return
        self.shown = True

        self._register_listeners()

        # Refresh when we're displayed.
        self._async_refresh()

        super(KeyframeNamingWindow, self).showEvent(event)

    def hideEvent(self, event):
        if not self.shown:
            return
        self.shown = False

        self._unregister_listeners()
        super(KeyframeNamingWindow, self).hideEvent(event)

    def dockCloseEventTriggered(self):
        # Bug workaround: closing the dialog by clicking X doesn't call closeEvent.
        self.cleanup()
    
    def close(self):
        self.cleanup()
        super(KeyframeNamingWindow, self).close()
