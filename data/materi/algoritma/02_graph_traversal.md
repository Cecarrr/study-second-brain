# Graph Traversal

## Breadth-First Search (BFS)
BFS uses a queue and visits nodes level by level, starting from the source node.
It finds the shortest path in an unweighted graph. Time complexity is O(V + E).

## Depth-First Search (DFS)
DFS uses a stack or recursion and goes as deep as possible before backtracking.
It is useful for cycle detection, topological sorting, and finding connected components. Time complexity is also O(V + E).

## When to use which
Use BFS when you need the fewest edges between two nodes. Use DFS when you need to explore all possibilities or check structure, such as cycles.
