from typing import List
import numpy as np
from sklearn.cluster import DBSCAN

from datapreparation.kitti360.imports import Object3d, Cell, Pose, DescriptionPoseCell, DescriptionBestCell
from datapreparation.kitti360.utils import STUFF_CLASSES
from datapreparation.kitti360.select import get_direction, get_direction_phi, select_objects_closest, select_objects_direction, select_objects_class, select_objects_random

from copy import deepcopy

def get_mask(points, cell_bbox):
    mask = np.bitwise_and.reduce((
        points[:, 0] >= cell_bbox[0],
        points[:, 1] >= cell_bbox[1],
        points[:, 2] >= cell_bbox[2],
        points[:, 0] <= cell_bbox[3],
        points[:, 1] <= cell_bbox[4],
        points[:, 2] <= cell_bbox[5],
    ))   
    return mask 

def cluster_stuff_object(obj, stuff_min, eps=0.75):
    """ Perform DBSCAN cluster, thresh objects by points again
    """
    # cluster = DBSCAN(eps=1.5, min_samples=300, leaf_size=30, n_jobs=-1).fit(obj.xyz)
    cluster = DBSCAN(eps=eps, n_jobs=-1).fit(obj.xyz)
    clustered_objects = []

    for i, label_value in enumerate(range(0, np.max(cluster.labels_) + 1)):
        mask = cluster.labels_ == label_value
        if np.sum(mask) < stuff_min:
            continue

        c_obj = obj.mask_points(mask)
        clustered_objects.append(c_obj)

    return clustered_objects    

def create_synthetic_cell(bbox_w, area_objects: List[Object3d], min_objects=6, inside_fraction=1/3):  
    """Creates a synthetic cell for use in synthetic fine-localization training.
    Only threshes the objects inside/outside.
    Performs no normalization and does not re-set IDs.

    Args:
        bbox_w ([type]): [description]
        area_objects (List[Object3d]): [description]
        min_objects ([type], optional): [description]. Defaults to 6.
        inside_fraction ([type], optional): [description]. Defaults to 1/3.
    """
    cell_objects = [obj for obj in area_objects]
    # for obj in area_objects:
    #     mask = get_mask(obj.xyz, bbox_w)
    #     if np.sum(mask) / len(mask) < inside_fraction:
    #         continue
    #     cell_objects.append(obj)

    cell_size = np.max(bbox_w[3:6] - bbox_w[0:3])

    if len(cell_objects) < min_objects:
        return None

    return Cell(-1, "mock", cell_objects, cell_size, bbox_w)

def create_cell(cell_idx, scene_name, bbox_w, scene_objects: List[Object3d], num_mentioned=6, inside_fraction=1/3, stuff_min=250): # Before: 500
    cell_objects = []
    for obj in scene_objects:
        assert obj.id < 1e7

        mask = get_mask(obj.xyz, bbox_w)
        if obj.label in STUFF_CLASSES:
            if np.sum(mask) < stuff_min:
                continue

            cell_obj = obj.mask_points(mask)
            clustered_objects = cluster_stuff_object(cell_obj, stuff_min)
            cell_objects.extend(clustered_objects)
        else:
            if np.sum(mask) / len(mask) < inside_fraction:
                continue
            cell_objects.append(deepcopy(obj)) # Deep-copy to avoid changing scene-objects multiple times

    # Normalize objects based on the largest cell-edge to be ∈ [0, 1] (instance-objects can reach over edge)
    cell_size = np.max(bbox_w[3:6] - bbox_w[0:3])
    for obj in cell_objects:
        obj.xyz = (obj.xyz - bbox_w[0:3]) / cell_size

    # else: # If cell is synthetic, only copy objects and set cell-size
    #     cell_objects = scene_objects
    #     cell_size = np.max(bbox_w[3:6] - bbox_w[0:3])

    if len(cell_objects) < num_mentioned:
        return None        

    # Reset all ids
    for id, obj in enumerate(cell_objects):
        obj.id = id   

    return Cell(cell_idx, scene_name, cell_objects, cell_size, bbox_w)

def describe_pose_in_pose_cell(pose_w, phi, cell: Cell, select_by, num_mentioned, max_dist=0.5) -> List[DescriptionPoseCell]:
    # Assert pose is close to cell_center
    # assert np.allclose(pose_w, cell.get_center())
    assert len(cell.objects) >= num_mentioned, f'Only {len(cell.objects)} objects, expected at least {num_mentioned}'

    # Norm pose
    pose = (pose_w - cell.bbox_w[0:3]) / cell.cell_size
    assert np.all(pose >= 0) and np.all(pose <= 1.0), f'{pose} {pose_w} {cell.bbox_w}'

    # Select candidates based on the distance
    dists = np.linalg.norm([obj.get_closest_point(pose) - pose for obj in cell.objects], axis=1)
    candidates = [cell.objects[i] for i in range(len(dists)) if dists[i] <= max_dist]
    if len(candidates) < num_mentioned:
        return None

    if select_by == 'closest':
        selected_objects = select_objects_closest(candidates, pose, num_mentioned)
    elif select_by == 'direction':
        selected_objects = select_objects_direction(candidates, pose, num_mentioned, phi)  
    elif select_by == 'class':
        selected_objects = select_objects_class(candidates, pose, num_mentioned)            
    elif select_by == 'random':
        selected_objects = select_objects_random(candidates, pose, num_mentioned)                    

    descriptions = []
    for obj in selected_objects:
        # direction = get_direction(obj, pose)
        direction, direction_phi = get_direction_phi(obj, pose, phi)

        closest_point = obj.get_closest_point(pose)

        offset_center = pose - obj.get_center()
        offset_closest = pose - closest_point
        # descriptions.append(DescriptionPoseCell(obj.id, obj.instance_id, obj.label, obj.get_color_rgb(), obj.get_color_text(), direction, offset_center, offset_closest, closest_point))
        descriptions.append(DescriptionPoseCell(obj, direction, offset_center, offset_closest, closest_point, phi, direction_phi))

    return descriptions

def ground_pose_to_best_cell(pose_w: np.ndarray, pose_cell_descriptions: List[DescriptionPoseCell], cell: Cell) -> List[DescriptionBestCell]:
    # Assert cell is valid for this pose
    assert np.all(pose_w >= cell.bbox_w[0:3]) and np.all(pose_w <= cell.bbox_w[3:6]), f'{pose_w}, {cell.bbox_w}'
    assert len(cell.objects) >= len(pose_cell_descriptions), f'Only {len(cell.objects)} objects'    

    # Norm pose
    pose = (pose_w - cell.bbox_w[0:3]) / cell.cell_size
    assert np.all(pose >= 0) and np.all(pose <= 1.0), f'{pose} {pose_w} {cell.bbox_w}'

    best_cell_descriptions = []

    num_unmatched = 0
    matched_object_ids = []
    # Match the descriptions to the objects in the given cell
    for descr in pose_cell_descriptions:
        # Gather objects that have the correct instance_id and have not been matched yet.
        candidates = [obj for obj in cell.objects if (obj.instance_id == descr.object_instance_id and obj.id not in matched_object_ids)] 

        if len(candidates) == 0: # The description is not matched anymore in the best cell
            best_cell_descriptions.append(DescriptionBestCell.from_unmatched(descr))
            num_unmatched += 1            
        else: 
            # The description is matched, try to project it to best-matching candidate
            # Select the candidate with the best-matching closest_offset
            # Closest_offset is least likely to have changed
            closest_offsets = np.array([pose - cand.get_closest_point(pose) for cand in candidates])[:, 0:2]
            best_idx = np.argmin(np.linalg.norm(closest_offsets - descr.offset_closest, axis=1))
            best_obj = candidates[best_idx]
            best_closest_offset = closest_offsets[best_idx]
            
            if np.linalg.norm(descr.offset_closest - best_closest_offset) > np.sqrt(2)/2: # Offsets are too different -> not a match. Allow some tolerance here for equally true matches.
                best_cell_descriptions.append(DescriptionBestCell.from_unmatched(descr))
                num_unmatched += 1
            else: # Offset is close -> object is matched               
                matched_object_ids.append(best_obj.id) # Prevent the object from being matched again
                
                # Calculating these 3 now for best-cell and saving them as well.
                closest_point = best_obj.get_closest_point(pose)
                best_offset_center = pose - best_obj.get_center() 
                best_offset_closest = pose - closest_point 

                best_cell_descriptions.append(DescriptionBestCell.from_matched(descr, best_obj.id, closest_point, best_offset_center, best_offset_closest))

    return best_cell_descriptions, pose, num_unmatched
    


    

