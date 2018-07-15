from UM.Preferences import Preferences

from UM.Math.Vector import Vector
from UM.Scene.Iterator.DepthFirstIterator import DepthFirstIterator
from UM.Operations.AddSceneNodeOperation import AddSceneNodeOperation

from cura.Scene.CuraSceneNode import CuraSceneNode

from cura.Scene.BuildPlateDecorator import BuildPlateDecorator
from cura.Scene.ConvexHullDecorator import ConvexHullDecorator
from cura.Scene.SliceableObjectDecorator import SliceableObjectDecorator
from cura.Scene.BlockSlicingDecorator import BlockSlicingDecorator

from cura.Arranging.Arrange import Arrange
from cura.Arranging.ShapeArray import ShapeArray

import os

class CuraApplicationPatches():
    def __init__(self, application):
        self._application = application

        self._application._readMeshFinished = self._readMeshFinished


    def _readMeshFinished(self, job):
        ### START PATCH: detect blackbelt printer
        global_container_stack = self._application.getGlobalContainerStack()
        if not global_container_stack:
            return

        definition_container = global_container_stack.getBottom()
        is_blackbelt_printer = definition_container.getId() == "blackbelt"
        ### END PATCH

        nodes = job.getResult()
        filename = job.getFileName()
        self._application._currently_loading_files.remove(filename)

        self._application.fileLoaded.emit(filename)
        arrange_objects_on_load = (
            not Preferences.getInstance().getValue("cura/use_multi_build_plate") or
            not Preferences.getInstance().getValue("cura/not_arrange_objects_on_load"))
        target_build_plate = self._application.getMultiBuildPlateModel().activeBuildPlate if arrange_objects_on_load else -1

        root = self._application.getController().getScene().getRoot()
        fixed_nodes = []
        for node_ in DepthFirstIterator(root):
            if node_.callDecoration("isSliceable") and node_.callDecoration("getBuildPlateNumber") == target_build_plate:
                fixed_nodes.append(node_)
        global_container_stack = self._application.getGlobalContainerStack()
        machine_width = global_container_stack.getProperty("machine_width", "value")
        machine_depth = global_container_stack.getProperty("machine_depth", "value")
        arranger = Arrange.create(x = machine_width, y = machine_depth, fixed_nodes = fixed_nodes)
        min_offset = 8
        default_extruder_position = self._application.getMachineManager().defaultExtruderPosition
        default_extruder_id = self._application._global_container_stack.extruders[default_extruder_position].getId()

        select_models_on_load = Preferences.getInstance().getValue("cura/select_models_on_load")

        for original_node in nodes:

            # Create a CuraSceneNode just if the original node is not that type
            if isinstance(original_node, CuraSceneNode):
                node = original_node
            else:
                node = CuraSceneNode()
                node.setMeshData(original_node.getMeshData())

                #Setting meshdata does not apply scaling.
                if(original_node.getScale() != Vector(1.0, 1.0, 1.0)):
                    node.scale(original_node.getScale())

            node.setSelectable(True)
            node.setName(os.path.basename(filename))
            self._application.getBuildVolume().checkBoundsAndUpdate(node)

            is_non_sliceable = False
            filename_lower = filename.lower()
            for extension in self._application._non_sliceable_extensions:
                if filename_lower.endswith(extension):
                    is_non_sliceable = True
                    break
            if is_non_sliceable:
                self._application.callLater(lambda: self._application.getController().setActiveView("SimulationView"))

                block_slicing_decorator = BlockSlicingDecorator()
                node.addDecorator(block_slicing_decorator)
            else:
                sliceable_decorator = SliceableObjectDecorator()
                node.addDecorator(sliceable_decorator)

            scene = self._application.getController().getScene()

            # If there is no convex hull for the node, start calculating it and continue.
            if not node.getDecorator(ConvexHullDecorator):
                node.addDecorator(ConvexHullDecorator())
            for child in node.getAllChildren():
                if not child.getDecorator(ConvexHullDecorator):
                    child.addDecorator(ConvexHullDecorator())

            ### START PATCH: don't do standard arrange on load for blackbelt printers
            ###              but place in a line instead
            if is_blackbelt_printer:
                margin_between_models = 50
                half_node_depth = node.getBoundingBox().depth / 2
                build_plate_empty = True
                leading_edge = self._application.getBuildVolume().getBoundingBox().front

                for existing_node in DepthFirstIterator(root):
                    if (
                        not issubclass(type(existing_node), CuraSceneNode) or
                        (not existing_node.getMeshData() and not existing_node.callDecoration("getLayerData")) or
                        (existing_node.callDecoration("getBuildPlateNumber") != target_build_plate)):

                        continue

                    build_plate_empty = False
                    leading_edge = min(leading_edge, existing_node.getBoundingBox().back)

                if not build_plate_empty or leading_edge < half_node_depth:
                    node.setPosition(Vector(0, 0, leading_edge - half_node_depth - margin_between_models ))

            ### END PATCH
            elif arrange_objects_on_load:
                if node.callDecoration("isSliceable"):
                    # Only check position if it's not already blatantly obvious that it won't fit.
                    if node.getBoundingBox() is None or self._application._volume.getBoundingBox() is None or node.getBoundingBox().width < self._application._volume.getBoundingBox().width or node.getBoundingBox().depth < self._application._volume.getBoundingBox().depth:
                        # Find node location
                        offset_shape_arr, hull_shape_arr = ShapeArray.fromNode(node, min_offset = min_offset)

                        # If a model is to small then it will not contain any points
                        if offset_shape_arr is None and hull_shape_arr is None:
                            Message(self._application._i18n_catalog.i18nc("@info:status", "The selected model was too small to load."),
                                    title=self._application._i18n_catalog.i18nc("@info:title", "Warning")).show()
                            return

                        # Step is for skipping tests to make it a lot faster. it also makes the outcome somewhat rougher
                        node, _ = arranger.findNodePlacement(node, offset_shape_arr, hull_shape_arr, step = 10)

            # This node is deep copied from some other node which already has a BuildPlateDecorator, but the deepcopy
            # of BuildPlateDecorator produces one that's associated with build plate -1. So, here we need to check if
            # the BuildPlateDecorator exists or not and always set the correct build plate number.
            build_plate_decorator = node.getDecorator(BuildPlateDecorator)
            if build_plate_decorator is None:
                build_plate_decorator = BuildPlateDecorator(target_build_plate)
                node.addDecorator(build_plate_decorator)
            build_plate_decorator.setBuildPlateNumber(target_build_plate)

            op = AddSceneNodeOperation(node, scene.getRoot())
            op.push()

            node.callDecoration("setActiveExtruder", default_extruder_id)
            scene.sceneChanged.emit(node)

            if select_models_on_load:
                Selection.add(node)

        self._application.fileCompleted.emit(filename)
