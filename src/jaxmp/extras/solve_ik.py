from typing import Literal

import jax
import jax.numpy as jnp
import jaxlie
import jaxls
import jax_dataclasses as jdc

from jaxmp.robot_factors import RobotFactors
from jaxmp.kinematics import JaxKinTree
from jaxmp.coll import RobotColl, CollGeom


@jdc.jit
def solve_ik(
    kin: JaxKinTree,
    target_pose: jaxlie.SE3,
    target_joint_indices: jax.Array,
    initial_pose: jnp.ndarray,
    JointVar: jdc.Static[type[jaxls.Var[jax.Array]]],
    ik_weight: jnp.ndarray,
    *,
    joint_var_idx: int = 0,
    rest_weight: float = 0.001,
    limit_weight: float = 100.0,
    joint_vel_weight: float = 0.0,
    dt: float = 0.01,
    use_manipulability: jdc.Static[bool] = False,
    manipulability_weight: float = 0.001,
    solver_type: jdc.Static[
        Literal["cholmod", "conjugate_gradient", "dense_cholesky"]
    ] = "conjugate_gradient",
    ConstrainedSE3Var: jdc.Static[type[jaxls.Var[jaxlie.SE3]] | None] = None,
    pose_var_idx: int = 0,
    max_iterations: int = 50,
) -> tuple[jaxlie.SE3, jnp.ndarray]:
    """
    Solve IK for the robot.
    Args:
        target_pose: Desired pose of the target joint, SE3 has batch axes (n_target,).
        target_joint_indices: Indices of the target joints, length n_target.
        initial_pose: Initial pose of the joints, used for joint velocity cost factor.
        JointVar: Joint variable type.
        ConstrainedSE3Var: Constrained SE3 variable type.
        joint_var_idx: Index for the joint variable.
        pose_var_idx: Index for the pose variable.
        ik_weight: Weight for the IK cost factor.
        rest_weight: Weight for the rest cost factor.
        limit_weight: Weight for the joint limit cost factor.
        joint_vel_weight: Weight for the joint velocity cost factor.
        solver_type: Type of solver to use.
        dt: Time step for the velocity cost factor.
        max_iterations: Maximum number of iterations for the solver.
        manipulability_weight: Weight for the manipulability cost factor.
    Returns:
        Base pose (jaxlie.SE3)
        Joint angles (jnp.ndarray)
    """
    # NOTE You can't add new factors on-the-fly with JIT, because:
    # - we'd want to pass in lists of jaxls.Factor objects
    # - but lists / tuples are static
    # - and ArrayImpl is not a valid type for a static argument.
    # (and you can't stack different Factor definitions, since it's a part of the treedef.)

    factors: list[jaxls.Factor] = [
        RobotFactors.limit_cost_factor(
            JointVar,
            joint_var_idx,
            kin,
            jnp.array([limit_weight] * kin.num_actuated_joints),
        ),
        RobotFactors.limit_vel_cost_factor(
            JointVar,
            joint_var_idx,
            kin,
            dt,
            jnp.array([joint_vel_weight] * kin.num_actuated_joints),
            prev_cfg=initial_pose,
        ),
        RobotFactors.rest_cost_factor(
            JointVar,
            joint_var_idx,
            jnp.array([rest_weight] * kin.num_actuated_joints),
        ),
    ]

    factors.append(
        RobotFactors.ik_cost_factor(
            JointVar,
            joint_var_idx,
            kin,
            target_pose,
            target_joint_indices,
            ik_weight,
            BaseConstrainedSE3VarType=ConstrainedSE3Var,
            base_se3_var_idx=pose_var_idx,
        ),
    )

    if use_manipulability:
        factors.append(
            RobotFactors.manipulability_cost_factor(
                JointVar,
                joint_var_idx,
                kin,
                target_joint_indices,
                manipulability_weight,
            )
        )

    joint_vars: list[jaxls.Var] = [JointVar(joint_var_idx)]
    joint_var_values: list[jaxls.Var | jaxls._variables.VarWithValue] = [
        JointVar(joint_var_idx).with_value(initial_pose)
    ]
    if ConstrainedSE3Var is not None and pose_var_idx is not None:
        joint_vars.append(ConstrainedSE3Var(pose_var_idx))
        joint_var_values.append(ConstrainedSE3Var(pose_var_idx))

    graph = jaxls.FactorGraph.make(
        factors,
        joint_vars,
        use_onp=False,
    )
    solution = graph.solve(
        linear_solver=solver_type,
        initial_vals=jaxls.VarValues.make(joint_var_values),
        trust_region=jaxls.TrustRegionConfig(),
        termination=jaxls.TerminationConfig(
            gradient_tolerance=1e-5,
            parameter_tolerance=1e-5,
            max_iterations=max_iterations,
        ),
        verbose=False,
    )

    if ConstrainedSE3Var is not None:
        base_pose = solution[ConstrainedSE3Var(0)]
    else:
        base_pose = jaxlie.SE3.identity()

    joints = solution[JointVar(0)]
    return base_pose, joints


@jdc.jit
def solve_ik_with_coll(
    kin: JaxKinTree,
    target_joint_indices: jnp.ndarray,
    target_poses: jaxlie.SE3,
    robot_coll: RobotColl,
    world_coll: list[CollGeom],
    initial_pose: jnp.ndarray,
    *,
    pos_weight: jdc.Static[float] = 5.0,
    rot_weight: jdc.Static[float] = 1.0,
    rest_weight: jdc.Static[float] = 0.001,
    limit_weight: jdc.Static[float] = 100.0,
    self_coll_weight: jdc.Static[float] = 5.0,
    world_coll_weight: jdc.Static[float] = 10.0,
) -> jnp.ndarray:
    # Create factor graph.
    factors: list[jaxls.Factor] = []

    JointVar = RobotFactors.get_var_class(kin, initial_pose)
    joint_var_idx = 0

    ik_weight = jnp.array([pos_weight] * 3 + [rot_weight] * 3)
    factors.extend(
        [
            RobotFactors.ik_cost_factor(
                JointVar,
                joint_var_idx,
                kin,
                target_poses,
                target_joint_indices,
                ik_weight,
            ),
            RobotFactors.rest_cost_factor(
                JointVar,
                joint_var_idx,
                jnp.array([rest_weight] * kin.num_actuated_joints),
            ),
            RobotFactors.limit_vel_cost_factor(
                JointVar,
                joint_var_idx,
                kin,
                0.1,
                jnp.array([limit_weight] * kin.num_actuated_joints),
                initial_pose,
            ),
            RobotFactors.limit_cost_factor(
                JointVar,
                joint_var_idx,
                kin,
                jnp.array([limit_weight] * kin.num_actuated_joints),
            ),
        ]
    )

    # Add collision factors.
    self_coll_factor = RobotFactors.self_coll_factor(
        JointVar, joint_var_idx, kin, robot_coll, 0.05, self_coll_weight
    )
    world_coll_factors = [
        RobotFactors.world_coll_factor(
            JointVar, joint_var_idx, kin, robot_coll, coll, 0.1, world_coll_weight
        )
        for coll in world_coll
    ]

    factors.append(self_coll_factor)
    factors.extend(world_coll_factors)

    # Solve IK.
    joint_vars = [JointVar(joint_var_idx)]
    graph = jaxls.FactorGraph.make(
        factors,
        joint_vars,
        use_onp=False,
    )
    solution = graph.solve(
        initial_vals=jaxls.VarValues.make(joint_vars),
        trust_region=jaxls.TrustRegionConfig(lambda_initial=1.0),
        termination=jaxls.TerminationConfig(
            gradient_tolerance=1e-5,
            parameter_tolerance=1e-5,
            max_iterations=50,
        ),
        verbose=False,
    )

    # Update visualization.
    joints = solution[JointVar(joint_var_idx)]
    return joints
